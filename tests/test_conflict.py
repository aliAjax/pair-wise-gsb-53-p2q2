import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'applicant_id': 'A-900', 'case_type': 'family', 'received_day': 100, 'deadline_days': 30,
               'response_day': 110, 'representation_active': True,
               'required_documents': ['passport', 'sponsor_letter'], 'sponsor_ids': ['S-900']}


class ConflictOfInterestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.intake = Actor("intake-user", "intake_officer")
        self.supervisor = Actor("super-user", "supervisor")
        # 申请人与 L-KIN 单向登记亲属关系；L-CLEAN 无冲突；A-COMP 与担保人共同公司
        self.service.upsert_person(self.intake, "A-900", {"name": "申请人甲", "relative_ids": ["L-KIN"]})
        self.service.upsert_person(self.intake, "S-900", {"name": "担保人乙", "company_ids": ["CO-1"]})
        self.service.upsert_person(self.intake, "L-KIN", {"name": "亲属律师"})
        self.service.upsert_person(self.intake, "L-CLEAN", {"name": "清白律师"})
        self.service.upsert_person(self.intake, "A-COMP", {"name": "同公司协办", "company_ids": ["CO-1"]})

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference="IMM-40001"):
        return self.service.create(self.intake, reference, CREATE_DATA)

    def test_person_profile_persisted_with_symmetric_kinship(self):
        person = self.service.get_person(self.intake, "L-KIN")
        self.assertEqual(person["name"], "亲属律师")
        self.assertIn("A-900", person["relative_ids"])

    def test_person_write_requires_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.upsert_person(Actor("x", "legal_rep"), "P", {"name": "谁"})

    def test_clean_assignment_stays_normal(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-CLEAN", "slot": "legal_rep"})
        self.assertEqual(record["state"], "draft")
        self.assertEqual(record["conflict"]["status"], "active")
        self.assertFalse(record["conflict"]["current"]["conflict_hit"])

    def test_kinship_hit_pauses_for_review(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep", "note": "收案指派"})
        self.assertEqual(record["state"], "conflict_review")
        pending = record["conflict"]["pending"]
        self.assertTrue(pending["conflict_hit"])
        self.assertEqual(pending["conflict_sources"][0]["type"], "kinship")
        self.assertEqual(pending["conflict_sources"][0]["party_id"], "A-900")
        self.assertEqual(pending["note"], "收案指派")
        # 待复核期间普通业务动作被状态机挡住
        with self.assertRaises(Conflict):
            self.service.act(Actor("r", "legal_rep"), record["id"], record["version"], "submit",
                             {"documents": ["passport", "sponsor_letter"]})

    def test_only_supervisor_with_reason_can_release(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep"})
        with self.assertRaises(PermissionDenied):
            self.service.act(self.intake, record["id"], record["version"], "release_conflict",
                             {"release_reason": "放行"})
        with self.assertRaises(ValidationError):
            self.service.act(self.supervisor, record["id"], record["version"], "release_conflict",
                             {"release_reason": "   "})
        record = self.service.act(self.supervisor, record["id"], record["version"], "release_conflict",
                                  {"release_reason": "关系已申报并回避审批", "review_opinion": "同意"})
        self.assertEqual(record["state"], "draft")
        current = record["conflict"]["current"]
        self.assertEqual(current["status"], "released")
        self.assertEqual(current["released_by"], "super-user")
        self.assertEqual(record["conflict"]["releases"][0]["release_reason"], "关系已申报并回避审批")

    def test_shared_company_hit_then_reject_keeps_original_assignment(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-CLEAN", "slot": "legal_rep"})
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "A-COMP", "slot": "assistant"})
        self.assertEqual(record["state"], "conflict_review")
        source = record["conflict"]["pending"]["conflict_sources"][0]
        self.assertEqual(source["type"], "shared_company")
        self.assertEqual(source["company_id"], "CO-1")
        self.assertEqual(source["party_id"], "S-900")
        record = self.service.act(self.supervisor, record["id"], record["version"], "reject_conflict",
                                  {"reject_reason": "公司利益未隔离"})
        self.assertEqual(record["state"], "draft")
        self.assertEqual(record["conflict"]["current"]["holder_id"], "L-CLEAN")
        statuses = [item["status"] for item in record["conflict"]["history"]]
        self.assertEqual(statuses, ["active", "rejected"])

    def test_reassign_rechecks_and_preserves_history(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-CLEAN", "slot": "legal_rep"})
        # 换给有冲突的人 -> 重新检查，命中待复核；原指派保留
        record = self.service.act(Actor("co", "case_officer"), record["id"], record["version"], "reassign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep"})
        self.assertEqual(record["state"], "conflict_review")
        statuses = [item["status"] for item in record["conflict"]["history"]]
        self.assertEqual(statuses, ["superseded", "pending"])
        self.assertEqual(record["conflict"]["history"][0]["superseded_by"], "L-KIN")
        # 放行后回到原业务状态（此处为 draft）
        record = self.service.act(self.supervisor, record["id"], record["version"], "release_conflict",
                                  {"release_reason": "换人后复核放行"})
        self.assertEqual(record["state"], "draft")
        self.assertEqual(record["conflict"]["current"]["holder_id"], "L-KIN")

    def test_reassign_while_pending_replaces_without_second_pending(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep"})
        # 待复核阶段直接换人（主管/收案），旧待复核变为 replaced
        record = self.service.act(self.supervisor, record["id"], record["version"], "reassign",
                                  {"holder_id": "L-CLEAN", "slot": "legal_rep"})
        self.assertEqual(record["state"], "draft")
        statuses = [item["status"] for item in record["conflict"]["history"]]
        self.assertEqual(statuses, ["replaced", "active"])

    def test_unknown_assignee_rejected(self):
        record = self._create()
        with self.assertRaises(ValidationError):
            self.service.act(self.intake, record["id"], record["version"], "assign",
                             {"holder_id": "NOBODY", "slot": "legal_rep"})

    def test_reopen_rechecks_conflict(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-CLEAN", "slot": "legal_rep"})
        for action, role, data in [
            ('submit', 'legal_rep', {'documents': ['passport', 'sponsor_letter']}),
            ('request_evidence', 'case_officer', {'evidence_request_day': 115, 'allowed_days': 10, 'evidence_request': '补收入'}),
            ('respond', 'legal_rep', {'response_day': 120, 'documents': ['income_proof']}),
            ('decide', 'case_officer', {'decision': 'granted', 'decision_reason': '材料充分'}),
            ('close', 'supervisor', {'closure_note': '归档'}),
        ]:
            record = self.service.act(Actor("op", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "closed")
        # 无冲突 -> 直接重开
        record = self.service.act(self.supervisor, record["id"], record["version"], "reopen",
                                  {"reopen_reason": "新证据"})
        self.assertEqual(record["state"], "submitted")
        # 换为有冲突的律师并放行，再关闭重开 -> 重新核验命中
        record = self.service.act(Actor("co", "case_officer"), record["id"], record["version"], "reassign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep"})
        record = self.service.act(self.supervisor, record["id"], record["version"], "release_conflict",
                                  {"release_reason": "首次放行"})
        record = self.service.act(Actor("co", "case_officer"), record["id"], record["version"], "decide",
                                  {"decision": "granted", "decision_reason": "ok"})
        record = self.service.act(self.supervisor, record["id"], record["version"], "close",
                                  {"closure_note": "再归档"})
        record = self.service.act(self.supervisor, record["id"], record["version"], "reopen",
                                  {"reopen_reason": "复审"})
        self.assertEqual(record["state"], "conflict_review")
        self.assertTrue(record["conflict"]["pending"]["conflict_hit"])
        # 原指派与历次复核意见均保留
        releases = record["conflict"]["releases"]
        self.assertTrue(any(item["release_reason"] == "首次放行" for item in releases))

    def test_timeline_records_review_decisions(self):
        record = self._create()
        record = self.service.act(self.intake, record["id"], record["version"], "assign",
                                  {"holder_id": "L-KIN", "slot": "legal_rep"})
        self.service.act(self.supervisor, record["id"], record["version"], "release_conflict",
                         {"release_reason": "时间线留痕"})
        timeline = self.service.timeline(self.intake, record["id"])
        self.assertEqual([item["action"] for item in timeline], ["created", "assign", "release_conflict"])
        self.assertIn("待复核", timeline[1]["details"]["summary"])
