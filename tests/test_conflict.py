"""利益冲突核验：人员关系、收案/换人指派、待复核与放行、重新核验。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from src.rules import DomainRules


BASE_CASE = {
    'applicant_id': 'A-900',
    'case_type': 'family',
    'received_day': 100,
    'deadline_days': 30,
    'response_day': 110,
    'representation_active': True,
    'required_documents': ['passport', 'sponsor_letter'],
    'sponsor_id': 'S-100',
    'applicant_company_ids': ['CO-FAMILY'],
    'sponsor_company_ids': ['CO-SPONSOR'],
}

OFFICER = lambda uid: Actor(uid, 'intake_officer')
SUPERVISOR = Actor('boss', 'supervisor')
CASE_OFFICER = Actor('officer', 'case_officer')


def make_service():
    temp = tempfile.TemporaryDirectory()
    service = build_service(str(Path(temp.name) / 'test.db'))
    return temp, service


class ConflictRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()
        self.payload = {
            'applicant_id': 'A-1', 'sponsor_id': 'S-1',
            'applicant_company_ids': ['CO-A'], 'sponsor_company_ids': ['CO-S'],
        }

    def test_family_conflict_for_applicant_and_sponsor(self):
        person = {'person_id': 'L-1', 'name': '律师', 'relatives': [
            {'other_party_id': 'A-1', 'other_party_kind': 'applicant', 'relation': '夫妻'},
            {'other_party_id': 'S-1', 'other_party_kind': 'sponsor', 'relation': '父子'},
        ], 'companies': []}
        conflicts = self.rules.check_interest_conflicts(person, self.payload)
        self.assertEqual([c['type'] for c in conflicts], ['family', 'family'])
        self.assertEqual({c['target'] for c in conflicts}, {'applicant', 'sponsor'})

    def test_company_conflict(self):
        person = {'person_id': 'L-2', 'name': '律师', 'relatives': [], 'companies': [
            {'company_id': 'CO-S', 'company_name': '担保公司', 'relation': '股东'},
        ]}
        conflicts = self.rules.check_interest_conflicts(person, self.payload)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['type'], 'company')
        self.assertEqual(conflicts[0]['target'], 'sponsor')
        self.assertIn('CO-S', conflicts[0]['description'])

    def test_no_relation_no_conflict(self):
        person = {'person_id': 'L-3', 'name': '律师', 'relatives': [
            {'other_party_id': 'OTHER', 'other_party_kind': 'applicant', 'relation': '兄弟'},
        ], 'companies': [{'company_id': 'CO-OTHER', 'company_name': '', 'relation': ''}]}
        self.assertEqual(self.rules.check_interest_conflicts(person, self.payload), [])

    def test_optional_party_fields(self):
        prepared = self.rules.prepare_create(dict(BASE_CASE))
        self.assertEqual(prepared['sponsor_id'], 'S-100')
        minimal = {k: v for k, v in BASE_CASE.items() if k not in {'sponsor_id', 'sponsor_company_ids', 'applicant_company_ids', 'lead_person_id', 'assistant_person_id'}}
        prepared_min = self.rules.prepare_create(minimal)
        self.assertEqual(prepared_min['sponsor_company_ids'], [])

    def test_sponsor_company_without_sponsor_rejected(self):
        bad = {k: v for k, v in BASE_CASE.items() if k != 'sponsor_id'}
        with self.assertRaises(ValidationError):
            self.rules.prepare_create(bad)


class AssignmentWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp, self.service = make_service()
        # 与申请人是夫妻的主办、与担保人有共同公司的协办、无冲突的律师
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-FAM', 'name': '亲属律师', 'relatives': [
            {'other_party_id': 'A-900', 'other_party_kind': 'applicant', 'relation': '夫妻'},
        ], 'companies': []})
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-CO', 'name': '公司律师', 'relatives': [], 'companies': [
            {'company_id': 'CO-SPONSOR', 'company_name': '担保企业', 'relation': '董事'},
        ]})
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-OK', 'name': '无冲突律师', 'relatives': [], 'companies': []})

    def tearDown(self):
        self.temp.cleanup()

    def test_intake_assignment_without_conflict_is_active(self):
        data = dict(BASE_CASE, lead_person_id='LAW-OK')
        record = self.service.create(OFFICER('c1'), 'IMM-30001', data)
        self.assertFalse(record['conflict_pending'])
        self.assertEqual(record['active_assignments']['lead']['status'], 'active')

    def test_intake_conflict_stops_at_pending_review(self):
        data = dict(BASE_CASE, lead_person_id='LAW-FAM', assistant_person_id='LAW-CO')
        record = self.service.create(OFFICER('c1'), 'IMM-30002', data)
        self.assertTrue(record['conflict_pending'])
        lead = record['active_assignments'].get('lead')
        self.assertIsNone(lead)
        pending = {a['role']: a for a in record['assignments'] if a['status'] == 'pending_review'}
        self.assertEqual(set(pending), {'lead', 'assistant'})
        self.assertEqual(pending['lead']['conflicts'][0]['type'], 'family')
        self.assertEqual(pending['assistant']['conflicts'][0]['type'], 'company')
        # 待复核期间业务动作被拦截
        with self.assertRaises(Conflict):
            self.service.act(Actor('op', 'legal_rep'), record['id'], record['version'], 'submit',
                             {'documents': ['passport', 'sponsor_letter']})

    def test_supervisor_approval_with_reason_releases_case(self):
        data = dict(BASE_CASE, lead_person_id='LAW-FAM')
        record = self.service.create(OFFICER('c1'), 'IMM-30003', data)
        pending = [a for a in record['assignments'] if a['status'] == 'pending_review'][0]
        # 不收案人员不能复核
        with self.assertRaises(PermissionDenied):
            self.service.review_assignment(OFFICER('c1'), pending['id'], {'decision': 'approved', 'reason': 'ok'})
        # 必须写明理由
        with self.assertRaises(ValidationError):
            self.service.review_assignment(SUPERVISOR, pending['id'], {'decision': 'approved', 'reason': '   '})
        record = self.service.review_assignment(SUPERVISOR, pending['id'], {
            'decision': 'approved', 'reason': '已核实双方已解除婚姻财产关联，主管批准继续代理',
        })
        self.assertFalse(record['conflict_pending'])
        lead = record['active_assignments']['lead']
        self.assertEqual(lead['status'], 'active')
        self.assertEqual(lead['latest_review']['decision'], 'approved')
        self.assertTrue(lead['latest_review']['reason'])
        # 放行后普通流程可继续
        record = self.service.act(Actor('op', 'legal_rep'), record['id'], record['version'], 'submit',
                                  {'documents': ['passport', 'sponsor_letter']})
        self.assertEqual(record['state'], 'submitted')

    def test_reject_keeps_case_blocked_and_reassign_rechecks(self):
        data = dict(BASE_CASE, lead_person_id='LAW-FAM')
        record = self.service.create(OFFICER('c1'), 'IMM-30004', data)
        pending = [a for a in record['assignments'] if a['status'] == 'pending_review'][0]
        record = self.service.review_assignment(SUPERVISOR, pending['id'], {
            'decision': 'rejected', 'reason': '配偶关系无法隔离，要求换人',
        })
        # rejected不构成待复核，但该席位尚无有效指派
        self.assertFalse(record['conflict_pending'])
        self.assertNotIn('lead', record['active_assignments'])
        self.assertEqual(record['assignments'][-1]['status'], 'rejected')
        with self.assertRaises(Conflict):
            self.service.act(Actor('op', 'legal_rep'), record['id'], record['version'], 'submit',
                             {'documents': ['passport', 'sponsor_letter']})
        # 换人：换成无冲突律师。驳回记录原样保留，新指派对席位生效
        record = self.service.assign(OFFICER('c1'), record['id'], {'role': 'lead', 'person_id': 'LAW-OK'})
        statuses = [(a['person_id'], a['status']) for a in record['assignments']]
        self.assertEqual(statuses[0], ('LAW-FAM', 'rejected'))
        self.assertEqual(statuses[1], ('LAW-OK', 'active'))
        self.assertFalse(record['conflict_pending'])
        # 再换回冲突律师，重新检查命中待复核
        record = self.service.assign(OFFICER('c1'), record['id'], {'role': 'lead', 'person_id': 'LAW-FAM'})
        self.assertTrue(record['conflict_pending'])
        self.assertEqual(record['assignments'][-1]['status'], 'pending_review')

    def test_recheck_after_relation_update_and_reopen(self):
        data = dict(BASE_CASE, lead_person_id='LAW-OK')
        record = self.service.create(OFFICER('c1'), 'IMM-30005', data)
        assignment_id = record['active_assignments']['lead']['id']
        # 人员资料新增与申请人的共同公司：active被打回待复核
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-OK', 'name': '无冲突律师', 'relatives': [], 'companies': [
            {'company_id': 'CO-FAMILY', 'company_name': '家族企业', 'relation': '股东'},
        ]})
        record = self.service.recheck_assignment(CASE_OFFICER, assignment_id)
        self.assertTrue(record['conflict_pending'])
        assignment = [a for a in record['assignments'] if a['id'] == assignment_id][0]
        self.assertEqual(assignment['conflicts'][0]['type'], 'company')
        # 关系解除后重新核验，待复核仍需主管放行（不自动跳过复核环节）
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-OK', 'name': '无冲突律师', 'relatives': [], 'companies': []})
        record = self.service.recheck_assignment(CASE_OFFICER, assignment_id)
        assignment = [a for a in record['assignments'] if a['id'] == assignment_id][0]
        self.assertEqual(assignment['status'], 'pending_review')
        self.assertEqual(assignment['conflicts'], [])
        record = self.service.review_assignment(SUPERVISOR, assignment_id, {
            'decision': 'approved', 'reason': '公司关联已注销，重新核验无冲突，放行',
        })
        # 复核意见全程保留：先有一次无意见的空转，这里至少有一条放行记录
        self.assertEqual(record['active_assignments']['lead']['latest_review']['reason'],
                         '公司关联已注销，重新核验无冲突，放行')

    def test_recheck_works_after_case_closed(self):
        data = dict(BASE_CASE, lead_person_id='LAW-OK', required_documents=['passport'])
        record = self.service.create(OFFICER('c1'), 'IMM-30006', data)
        record = self.service.act(Actor('op', 'legal_rep'), record['id'], record['version'], 'submit',
                                  {'documents': ['passport']})
        record = self.service.act(Actor('dec', 'case_officer'), record['id'], record['version'], 'decide',
                                  {'decision': 'granted', 'decision_reason': '通过'})
        record = self.service.act(Actor('boss2', 'supervisor'), record['id'], record['version'], 'close',
                                  {'closure_note': '归档'})
        self.assertEqual(record['state'], 'closed')
        assignment_id = record['active_assignments']['lead']['id']
        self.service.upsert_person(SUPERVISOR, {'person_id': 'LAW-OK', 'name': '无冲突律师', 'relatives': [
            {'other_party_id': 'A-900', 'other_party_kind': 'applicant', 'relation': '兄妹'},
        ], 'companies': []})
        record = self.service.recheck_assignment(SUPERVISOR, assignment_id)
        self.assertEqual(record['assignments'][-1]['status'], 'pending_review')
        self.assertEqual(record['assignments'][-1]['conflicts'][0]['relation'], '兄妹')

    def test_audit_trail_keeps_assignment_and_review_history(self):
        data = dict(BASE_CASE, lead_person_id='LAW-FAM')
        record = self.service.create(OFFICER('c1'), 'IMM-30007', data)
        pending = [a for a in record['assignments'] if a['status'] == 'pending_review'][0]
        # 主管驳回，随后换人；原指派与复核意见都在
        self.service.review_assignment(SUPERVISOR, pending['id'], {'decision': 'rejected', 'reason': '驳回换人'})
        self.service.assign(OFFICER('c1'), record['id'], {'role': 'lead', 'person_id': 'LAW-OK'})
        # 再直接替换进行中的指派，旧记录标记removed并留下替换审计
        self.service.assign(OFFICER('c1'), record['id'], {'role': 'lead', 'person_id': 'LAW-FAM'})
        timeline = self.service.timeline(OFFICER('c1'), record['id'])
        actions = [event['action'] for event in timeline]
        self.assertIn('assign_conflict', actions)
        self.assertIn('assignment_reviewed', actions)
        self.assertIn('assign', actions)
        self.assertIn('assignment_superseded', actions)
        detail = self.service.get_record(SUPERVISOR, record['id'])
        first = detail['assignments'][0]
        self.assertEqual(first['status'], 'rejected')
        self.assertEqual(first['latest_review']['reason'], '驳回换人')

    def test_intake_unknown_person_rejected(self):
        data = dict(BASE_CASE, lead_person_id='NOBODY')
        with self.assertRaises(NotFound):
            self.service.create(OFFICER('c1'), 'IMM-30008', data)

    def test_only_authorized_roles_manage_and_assign(self):
        with self.assertRaises(PermissionDenied):
            self.service.upsert_person(Actor('x', 'legal_rep'), {'person_id': 'P', 'name': 'p'})
        record = self.service.create(OFFICER('c1'), 'IMM-30009', dict(BASE_CASE))
        with self.assertRaises(PermissionDenied):
            self.service.assign(Actor('x', 'legal_rep'), record['id'], {'role': 'lead', 'person_id': 'LAW-OK'})


if __name__ == '__main__':
    unittest.main()
