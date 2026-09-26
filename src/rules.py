"""移民案件期限与材料管理领域规则与状态转换。"""
import re
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}

# 利益冲突核验相关常量
PARTY_KINDS = ('applicant', 'sponsor')
PARTY_LABELS = {'applicant': '申请人', 'sponsor': '担保人'}
ASSIGNMENT_ROLES = ('lead', 'assistant')
ASSIGNMENT_ROLE_LABELS = {'lead': '主办律师', 'assistant': '协办'}
REVIEW_DECISIONS = ('approved', 'rejected')
PERSON_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_manage_person(self, role: str) -> bool:
        return role == "admin" or role == "supervisor"

    def role_can_assign(self, role: str) -> bool:
        return role == "admin" or role in {'intake_officer', 'supervisor'}

    def role_can_review_conflict(self, role: str) -> bool:
        return role == "admin" or role == "supervisor"

    def role_can_recheck(self, role: str) -> bool:
        return role == "admin" or role in {'intake_officer', 'supervisor', 'case_officer'}

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        # 担保人及其公司、收案指派均为可选字段：不填时沿用原有普通收案流程
        p["sponsor_id"] = optional_text(p, "sponsor_id")
        p["applicant_company_ids"] = text_list(p, "applicant_company_ids")
        p["sponsor_company_ids"] = text_list(p, "sponsor_company_ids")
        if p["sponsor_company_ids"] and not p["sponsor_id"]:
            raise ValidationError("填写担保人公司前必须先提供sponsor_id")
        p["lead_person_id"] = optional_text(p, "lead_person_id")
        p["assistant_person_id"] = optional_text(p, "assistant_person_id")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        return p

    def prepare_person(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """校验并规整人员资料：人员基本信息、与申请人/担保人的亲属关系、共同公司。"""
        p = payload or {}
        person_id = text(p, "person_id")
        if not PERSON_ID_RE.match(person_id):
            raise ValidationError("person_id只能包含字母、数字、下划线或连字符")
        data: Dict[str, Any] = {"person_id": person_id, "name": text(p, "name"), "title": optional_text(p, "title"), "relatives": [], "companies": []}
        relatives = p.get("relatives", [])
        if not isinstance(relatives, list):
            raise ValidationError("relatives必须是列表")
        seen_relative = set()
        for item in relatives:
            if not isinstance(item, dict):
                raise ValidationError("relatives项必须是对象")
            other_id = text(item, "other_party_id")
            kind = choice(item, "other_party_kind", list(PARTY_KINDS))
            relation = text(item, "relation")
            key = (kind, other_id)
            if key in seen_relative:
                continue
            seen_relative.add(key)
            data["relatives"].append({"other_party_id": other_id, "other_party_kind": kind, "relation": relation})
        companies = p.get("companies", [])
        if not isinstance(companies, list):
            raise ValidationError("companies必须是列表")
        seen_company = set()
        for item in companies:
            if not isinstance(item, dict):
                raise ValidationError("companies项必须是对象")
            company_id = text(item, "company_id")
            if company_id in seen_company:
                continue
            seen_company.add(company_id)
            data["companies"].append({"company_id": company_id, "company_name": optional_text(item, "company_name"), "relation": optional_text(item, "relation")})
        return data

    def check_interest_conflicts(self, person: Dict[str, Any], case_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """核对人员与案件申请人/担保人的利益冲突：亲属关系或共同公司。

        返回命中的冲突来源列表；空列表表示无冲突。人员资料里的关系是事实来源，
        不再依赖案件备注人工判断。
        """
        parties = [("applicant", str(case_payload.get("applicant_id", "")), list(case_payload.get("applicant_company_ids", [])))]
        sponsor_id = str(case_payload.get("sponsor_id", ""))
        if sponsor_id:
            parties.append(("sponsor", sponsor_id, list(case_payload.get("sponsor_company_ids", []))))
        company_map = {item["company_id"]: item for item in person.get("companies", [])}
        conflicts: List[Dict[str, Any]] = []
        for target, party_id, party_company_ids in parties:
            party_label = PARTY_LABELS[target]
            for rel in person.get("relatives", []):
                if rel["other_party_kind"] == target and rel["other_party_id"] == party_id:
                    conflicts.append({
                        "type": "family",
                        "target": target,
                        "target_id": party_id,
                        "relation": rel["relation"],
                        "description": "与%s%s存在%s关系" % (party_label, party_id, rel["relation"]),
                    })
            for company_id in party_company_ids:
                shared = company_map.get(company_id)
                if shared is None:
                    continue
                description = "与%s%s存在共同公司%s" % (party_label, party_id, company_id)
                if shared.get("company_name"):
                    description += "（%s）" % shared["company_name"]
                conflicts.append({
                    "type": "company",
                    "target": target,
                    "target_id": party_id,
                    "company_id": company_id,
                    "company_name": shared.get("company_name", ""),
                    "person_relation": shared.get("relation", ""),
                    "description": description,
                })
        return conflicts

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "submit":
            docs = text_list(data, "documents", 1)
            missing = [doc for doc in p["required_documents"] if doc not in docs]
            if missing and not boolean(data, "supervisor_waiver"):
                raise ValidationError("缺少材料：" + ", ".join(missing))
            if p["overdue"] and not boolean(data, "supervisor_waiver"):
                raise ValidationError("案件已超过提交期限")
            changes["submitted_documents"] = docs
            changes["missing_documents"] = missing
            changes["waiver_used"] = boolean(data, "supervisor_waiver")
            summary = "申请材料已提交"
        elif action == "request_evidence":
            request_day = integer(data, "evidence_request_day", p["response_day"])
            allowed_days = integer(data, "allowed_days", 1)
            changes["evidence_request_day"] = request_day
            changes["evidence_due_day"] = request_day + allowed_days
            changes["evidence_request"] = text(data, "evidence_request")
            summary = "补件要求已发出"
        elif action == "respond":
            docs = text_list(data, "documents", 1)
            if int(data.get("response_day", p["response_day"])) > int(p["evidence_due_day"]):
                raise ValidationError("补件回应超过期限")
            changes["response_day"] = int(data["response_day"])
            changes["evidence_documents"] = docs
            summary = "补件已回应"
        elif action == "decide":
            changes["decision"] = choice(data, "decision", ["granted", "denied", "withdrawn"])
            changes["decision_reason"] = text(data, "decision_reason")
            summary = "案件已作出决定"
        elif action == "appeal":
            appeal_day = integer(data, "appeal_day", 0)
            if appeal_day > int(p["deadline_day"]) + 30:
                raise ValidationError("上诉窗口已关闭")
            changes["appeal_day"] = appeal_day
            changes["appeal_reason"] = text(data, "appeal_reason")
            summary = "上诉已登记"
        elif action == "close":
            changes["closure_note"] = text(data, "closure_note")
            summary = "案件归档"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
