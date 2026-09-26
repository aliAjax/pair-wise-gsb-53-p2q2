"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import ACTIVE_ASSIGNMENT_STATUSES, ASSIGN_SLOTS, PERSON_WRITE_ROLES, REVIEW_STATE, DomainRules


CONFLICT_ACTIONS = {"assign", "reassign", "reopen"}
STATUS_LABELS = {
    "pending": "待复核",
    "active": "已指派",
    "released": "已放行",
    "rejected": "已驳回",
    "replaced": "已被换人",
    "superseded": "已被换人",
    "released_replaced": "已放行后被换人",
}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        return self._with_conflict_view(record)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._with_conflict_view(self.repository.get(record_id))

    # -- 人员资料 ------------------------------------------------------------

    def upsert_person(self, actor: Actor, person_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.role not in PERSON_WRITE_ROLES:
            raise PermissionDenied("角色无权维护人员资料")
        person_id = text({"person_id": person_id}, "person_id")
        profile = self.rules.validate_person(payload or {})
        return self.repository.upsert_person(person_id, profile, actor.user_id)

    def get_person(self, actor: Actor, person_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        person = self.repository.get_person(text({"person_id": person_id}, "person_id"))
        if person is None:
            from .domain import NotFound
            raise NotFound("人员资料不存在")
        return person

    def list_persons(self, actor: Actor) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_persons()

    # -- 案件动作 ------------------------------------------------------------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        context = self._action_context(action, data or {}, record, actor)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, context)
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._with_conflict_view(result)

    def _case_party_ids(self, payload: Dict[str, Any]) -> List[str]:
        return [payload.get("applicant_id", "")] + list(payload.get("sponsor_ids", []))

    def _action_context(self, action: str, data: Dict[str, Any], record: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        context: Dict[str, Any] = {"actor_id": actor.user_id}
        payload = record["payload"]
        if action in ("assign", "reassign"):
            holder_id = text(data, "holder_id")
            parties = self.repository.persons_map(self._case_party_ids(payload) + [holder_id])
            if not parties.get(holder_id, {}).get("name"):
                raise ValidationError("被指派人%s尚未登记人员资料" % holder_id)
            context["conflict"] = self.rules.detect_interest_conflict(holder_id, parties)
            context["holder_name"] = parties[holder_id]["name"]
        elif action == "reopen":
            assignments = payload.get("assignments", [])
            holder_id = ""
            for entry in reversed(assignments):
                if entry.get("status") in ACTIVE_ASSIGNMENT_STATUSES:
                    holder_id = entry.get("holder_id", "")
                    break
            if holder_id:
                parties = self.repository.persons_map(self._case_party_ids(payload) + [holder_id])
                context["conflict"] = self.rules.detect_interest_conflict(holder_id, parties)
                context["holder_name"] = parties.get(holder_id, {}).get("name", "")
        return context

    # -- 冲突视图 ------------------------------------------------------------

    @staticmethod
    def _compact(entry: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "holder_id": entry.get("holder_id", ""),
            "holder_name": entry.get("holder_name", ""),
            "slot": entry.get("slot", ""),
            "status": entry.get("status", ""),
            "status_label": STATUS_LABELS.get(entry.get("status", ""), entry.get("status", "")),
            "note": entry.get("note", ""),
            "conflict_hit": entry.get("conflict_hit", False),
            "conflict_sources": entry.get("conflict_sources", []),
            "released_by": entry.get("released_by", ""),
            "release_reason": entry.get("release_reason", ""),
            "review_opinion": entry.get("review_opinion", ""),
            "rejected_by": entry.get("rejected_by", ""),
            "reject_reason": entry.get("reject_reason", ""),
            "superseded_by": entry.get("superseded_by", ""),
        }

    def _with_conflict_view(self, record: Dict[str, Any]) -> Dict[str, Any]:
        payload = record["payload"]
        assignments = payload.get("assignments", [])
        pending = next((entry for entry in assignments if entry.get("status") == "pending"), None)
        current = None
        for entry in reversed(assignments):
            if entry.get("status") in ACTIVE_ASSIGNMENT_STATUSES:
                current = entry
                break
        latest = assignments[-1] if assignments else None
        if record["state"] == REVIEW_STATE and pending is not None:
            view_status = "pending_review"
        elif latest is not None:
            view_status = latest.get("status", "")
        else:
            view_status = "unassigned"
        conflict = {
            "status": view_status,
            "status_label": {"pending_review": "待主管复核", "unassigned": "未指派"}.get(
                view_status, STATUS_LABELS.get(view_status, view_status)
            ),
            "under_review": record["state"] == REVIEW_STATE,
            "pending": self._compact(pending) if pending else None,
            "current": self._compact(current) if current else None,
            "releases": [
                self._compact(entry) for entry in assignments
                if entry.get("status") in ("released", "released_replaced") and entry.get("release_reason")
            ],
            "history": [self._compact(entry) for entry in assignments],
        }
        record["conflict"] = conflict
        return record

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
