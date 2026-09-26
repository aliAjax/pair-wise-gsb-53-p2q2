"""移民案件期限与材料管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
REVIEW_STATE = "conflict_review"
ASSIGN_SLOTS = ('legal_rep', 'assistant')
ACTIVE_ASSIGNMENT_STATUSES = ('active', 'released')
REPLACED_STATUSES = {'released': 'released_replaced'}
PERSON_WRITE_ROLES = {'intake_officer', 'supervisor'}
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'submit': {'legal_rep', 'case_officer'}, 'request_evidence': {'case_officer'}, 'respond': {'legal_rep'}, 'decide': {'case_officer', 'supervisor'}, 'appeal': {'legal_rep'}, 'close': {'supervisor'}, 'assign': {'intake_officer', 'supervisor'}, 'reassign': {'intake_officer', 'case_officer', 'supervisor'}, 'release_conflict': {'supervisor'}, 'reject_conflict': {'supervisor'}, 'reopen': {'supervisor'}}
TRANSITIONS = {'submit': {'draft': 'submitted'}, 'request_evidence': {'submitted': 'evidence_requested'}, 'respond': {'evidence_requested': 'response_received'}, 'decide': {'submitted': 'decided', 'response_received': 'decided'}, 'appeal': {'decided': 'appealed'}, 'close': {'decided': 'closed', 'appealed': 'closed'}}
REASSIGN_STATES = {'draft', 'submitted', 'evidence_requested', 'response_received', REVIEW_STATE}


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

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "applicant_id")
        choice(p, "case_type", ["asylum", "family", "work"])
        integer(p, "received_day", 0)
        integer(p, "deadline_days", 1)
        integer(p, "response_day", 0)
        boolean(p, "representation_active")
        text_list(p, "required_documents", 1)
        p["sponsor_ids"] = text_list(p, "sponsor_ids", 0)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["deadline_day"] = int(p["received_day"]) + int(p["deadline_days"])
        p["days_remaining"] = int(p["deadline_day"]) - int(p["response_day"])
        p["overdue"] = p["days_remaining"] < 0
        p["submitted_documents"] = []
        p["missing_documents"] = list(p["required_documents"])
        p["assignments"] = []
        return p

    def validate_person(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        name = text(p, "name")
        role = optional_text(p, "role", "")
        organization = optional_text(p, "organization", "")
        relatives = p.get("relative_ids", [])
        if relatives is None:
            relatives = []
        if not isinstance(relatives, list) or any(not isinstance(item, str) or not item.strip() for item in relatives):
            raise ValidationError("relative_ids必须是文本列表")
        companies = p.get("company_ids", [])
        if companies is None:
            companies = []
        if not isinstance(companies, list) or any(not isinstance(item, str) or not item.strip() for item in companies):
            raise ValidationError("company_ids必须是文本列表")
        return {"name": name.strip(), "role": role, "organization": organization,
                "relative_ids": sorted({item.strip() for item in relatives if item.strip()}),
                "company_ids": sorted({item.strip() for item in companies if item.strip()})}

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"closed", "decided"} and item["payload"].get("applicant_id") == payload.get("applicant_id") and item["payload"].get("case_type") == payload.get("case_type"):
                raise Conflict("同一申请人同类型案件仍在处理中")

    @staticmethod
    def detect_interest_conflict(holder_id: str, parties: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """对一名被指派人核验其与申请人/担保人的利益冲突。

        parties 为人员映射：person_id -> {"name", "relative_ids", "company_ids"}，
        其中 relative_ids 已由仓储层补成双向关系。
        """
        holder = parties.get(holder_id)
        sources = []
        if holder is not None:
            for party_id, party in parties.items():
                if party_id == holder_id:
                    continue
                if party_id in set(holder.get("relative_ids", [])):
                    sources.append({"type": "kinship", "party_id": party_id,
                                    "party_name": party.get("name", party_id),
                                    "detail": "与%s存在亲属关系" % party.get("name", party_id)})
                shared = sorted(set(holder.get("company_ids", [])) & set(party.get("company_ids", [])))
                for company_id in shared:
                    sources.append({"type": "shared_company", "party_id": party_id,
                                    "party_name": party.get("name", party_id), "company_id": company_id,
                                    "detail": "与%s共同任职公司%s" % (party.get("name", party_id), company_id)})
        return {"hit": bool(sources), "sources": sources}

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        state = record["state"]
        if action in ("release_conflict", "reject_conflict"):
            if state != REVIEW_STATE:
                raise Conflict("当前状态不允许执行%s" % action)
            return record["payload"].get("review_return_state", state)
        if action == "assign":
            if state != "draft":
                raise Conflict("当前状态不允许执行assign，如需换人请使用reassign")
            return state
        if action == "reassign":
            if state not in REASSIGN_STATES:
                raise Conflict("当前状态不允许执行reassign")
            return state
        if action == "reopen":
            if state != "closed":
                raise Conflict("当前状态不允许执行reopen")
            return "submitted"
        allowed = TRANSITIONS.get(action, {}).get(state)
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    @staticmethod
    def _assignments(record: Dict[str, Any]) -> list:
        p = record["payload"]
        p.setdefault("assignments", [])
        return p["assignments"]

    @staticmethod
    def _pending(assignments: list) -> Dict[str, Any]:
        for entry in assignments:
            if entry.get("status") == "pending":
                return entry
        return None

    @staticmethod
    def _active(assignments: list) -> Dict[str, Any]:
        for entry in reversed(assignments):
            if entry.get("status") in ACTIVE_ASSIGNMENT_STATUSES:
                return entry
        return None

    @staticmethod
    def _new_assignment(data: Dict[str, Any], check: Dict[str, Any], holder_name: str) -> Dict[str, Any]:
        return {
            "holder_id": text(data, "holder_id"),
            "holder_name": holder_name or data["holder_id"].strip(),
            "slot": choice(data, "slot", list(ASSIGN_SLOTS)),
            "note": optional_text(data, "note", ""),
            "status": "pending" if check["hit"] else "active",
            "conflict_hit": bool(check["hit"]),
            "conflict_sources": check["sources"],
            "released_by": "", "release_reason": "", "review_opinion": "",
            "rejected_by": "", "reject_reason": "",
            "superseded_by": "",
        }

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], context: Dict[str, Any] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        context = context or {}
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
        elif action in ("assign", "reassign"):
            assignments = list(p.get("assignments", []))
            replacing_pending = action == "reassign" and record["state"] == REVIEW_STATE and self._pending(assignments) is not None
            if action == "reassign" and not replacing_pending:
                if record["state"] == REVIEW_STATE:
                    raise Conflict("没有待复核的指派可替换")
                pending = self._pending(assignments)
                if pending is not None:
                    raise Conflict("有待复核的指派，需主管放行或驳回后才能再换人")
                previous = self._active(assignments)
                if previous is None:
                    raise Conflict("案件尚无原指派，应使用assign")
            check = context.get("conflict")
            if check is None:
                raise Conflict("缺少利益冲突核验结果")
            holder_id = text(data, "holder_id")
            entry = self._new_assignment(data, check, context.get("holder_name", ""))
            if action == "reassign":
                previous = self._pending(assignments) or self._active(assignments)
                if replacing_pending:
                    previous["status"] = "replaced"
                else:
                    # 已放行的指派被换人时保留放行依据，仅移出当前代理人
                    previous["status"] = REPLACED_STATUSES.get(previous.get("status"), "superseded")
                previous["superseded_by"] = holder_id
            assignments.append(entry)
            changes["assignments"] = assignments
            slot_label = "协办" if entry["slot"] == "assistant" else "律师"
            if check["hit"] and not replacing_pending:
                changes["review_return_state"] = record["state"]
                new_state = REVIEW_STATE
                summary = "%s%s的指派命中利益冲突，进入待复核" % (entry["holder_name"], slot_label)
            elif replacing_pending:
                if check["hit"]:
                    changes["review_return_state"] = record["payload"].get("review_return_state", "draft")
                    new_state = REVIEW_STATE
                    summary = "已更换为%s%s，但仍命中利益冲突，继续待复核" % (entry["holder_name"], slot_label)
                else:
                    new_state = record["payload"].get("review_return_state", "draft")
                    summary = "已更换为%s%s，核验无冲突，返回原处理状态" % (entry["holder_name"], slot_label)
            else:
                summary = ("已更换为%s%s" if action == "reassign" else "已指派%s%s") % (entry["holder_name"], slot_label)
        elif action in ("release_conflict", "reject_conflict"):
            assignments = list(p.get("assignments", []))
            pending = self._pending(assignments)
            if pending is None:
                raise Conflict("没有待复核的指派")
            reason_key = "release_reason" if action == "release_conflict" else "reject_reason"
            reason = text(data, reason_key)
            optional_text(data, "review_opinion", "")
            if action == "release_conflict":
                pending["status"] = "released"
                pending["released_by"] = context.get("actor_id", "")
                pending["release_reason"] = reason
                pending["review_opinion"] = optional_text(data, "review_opinion", "")
                summary = "主管已放行%s的指派：%s" % (pending["holder_name"], reason)
            else:
                pending["status"] = "rejected"
                pending["rejected_by"] = context.get("actor_id", "")
                pending["reject_reason"] = reason
                pending["review_opinion"] = optional_text(data, "review_opinion", "")
                new_state = record["payload"].get("review_return_state", "draft")
                summary = "主管驳回%s的指派：%s" % (pending["holder_name"], reason)
            changes["assignments"] = assignments
            changes["review_return_state"] = ""
        elif action == "reopen":
            note = optional_text(data, "reopen_reason", "")
            changes["reopen_note"] = note
            assignments = list(p.get("assignments", []))
            check = context.get("conflict")
            if check is not None and check["hit"]:
                current = self._active(assignments)
                if current is None:
                    raise Conflict("缺少重新核验的指派对象")
                entry = self._new_assignment(
                    {"holder_id": current["holder_id"], "slot": current.get("slot", "legal_rep"),
                     "note": "重开后重新核验"},
                    check, context.get("holder_name", current.get("holder_name", "")),
                )
                assignments.append(entry)
                changes["assignments"] = assignments
                changes["review_return_state"] = "closed"
                new_state = REVIEW_STATE
                summary = "案件重开，重新核验%s的指派发现利益冲突，进入待复核" % entry["holder_name"]
            else:
                summary = "案件已重开"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
