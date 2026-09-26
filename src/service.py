"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, choice, text
from .repository import Repository
from .rules import ASSIGNMENT_ROLES, DomainRules


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

    def _record_with_assignments(self, record: Dict[str, Any]) -> Dict[str, Any]:
        assignments = self.repository.list_assignments(int(record["id"]))
        record["assignments"] = assignments
        active = {item["role"]: item for item in assignments if item["status"] == "active"}
        record["conflict_pending"] = any(item["status"] == "pending_review" for item in assignments)
        record["active_assignments"] = active
        return record

    def _create_assignment(self, record_id: int, role: str, person_id: str, record: Dict[str, Any], actor: Actor, source: str) -> Dict[str, Any]:
        """指派前先做利益冲突核验：命中则停在待复核，等待主管写明理由放行。"""
        person = self.repository.get_person(person_id)
        conflicts = self.rules.check_interest_conflicts(person, record["payload"])
        status = "pending_review" if conflicts else "active"
        assignment = self.repository.insert_assignment(
            record_id=record_id,
            role=role,
            person_id=person_id,
            person_name=person.get("name", ""),
            status=status,
            conflicts=conflicts,
            note=source,
            actor_id=actor.user_id,
        )
        self.audit.note(
            record_id,
            actor.user_id,
            "assign" if not conflicts else "assign_conflict",
            {
                "assignment_id": assignment["id"],
                "role": role,
                "person_id": person_id,
                "status": status,
                "source": source,
                "conflicts": conflicts,
            },
        )
        return assignment

    def upsert_person(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_manage_person(actor.role):
            raise PermissionDenied("只有主管可以维护人员资料")
        data = self.rules.prepare_person(payload or {})
        return self.repository.upsert_person(data, actor.user_id)

    def list_persons(self, actor: Actor, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_persons(limit=limit)

    def get_person(self, actor: Actor, person_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_person(person_id)

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        # 收案即指派：先确认人员资料存在，落案后在同一流程内完成利益冲突核验
        intake_assign = False
        if prepared.get("lead_person_id") or prepared.get("assistant_person_id"):
            if not self.rules.role_can_assign(actor.role):
                raise PermissionDenied("角色无权在收案时指派")
            for person_id in (prepared.get("lead_person_id"), prepared.get("assistant_person_id")):
                if person_id:
                    self.repository.get_person(person_id)
            intake_assign = True
        record = self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
        if intake_assign:
            if prepared.get("lead_person_id"):
                self._create_assignment(record["id"], "lead", prepared["lead_person_id"], record, actor, "intake")
            if prepared.get("assistant_person_id"):
                self._create_assignment(record["id"], "assistant", prepared["assistant_person_id"], record, actor, "intake")
            record = self.repository.get(record["id"])
        return self._record_with_assignments(record)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._record_with_assignments(self.repository.get(record_id))

    def assign(self, actor: Actor, record_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """指派/换人：先核验冲突；换人后原指派保留并标记为removed，重新检查。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_assign(actor.role):
            raise PermissionDenied("角色无权指派或换人")
        record = self.repository.get(record_id)
        role = choice(payload or {}, "role", list(ASSIGNMENT_ROLES))
        person_id = text(payload or {}, "person_id")
        person = self.repository.get_person(person_id)
        current = self.repository.latest_assignment(record_id, role)
        if current is not None and current["person_id"] == person_id and current["status"] in {"active", "pending_review"}:
            raise ValidationError("该人员已担任本案%s，无需重复指派" % role)
        assignment = self._create_assignment(record_id, role, person_id, record, actor, "reassign")
        if current is not None and current["status"] in {"active", "pending_review"}:
            self.repository.mark_assignment_superseded(
                current["id"], assignment["id"], "被%s的新指派替换" % person_id
            )
            self.audit.note(
                record_id,
                actor.user_id,
                "assignment_superseded",
                {"assignment_id": current["id"], "superseded_by": assignment["id"], "role": role},
            )
        record = self.repository.get(record_id)
        return self._record_with_assignments(record)

    def review_assignment(self, actor: Actor, assignment_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        """主管对命中冲突的指代表明理由后放行或驳回。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_review_conflict(actor.role):
            raise PermissionDenied("只有主管可以复核利益冲突指派")
        assignment = self.repository.get_assignment(assignment_id)
        if assignment["status"] != "pending_review":
            raise ValidationError("该指派不在待复核状态")
        decision = choice(payload or {}, "decision", ["approved", "rejected"])
        reason = text(payload or {}, "reason")
        status = "active" if decision == "approved" else "rejected"
        updated = self.repository.review_assignment(assignment_id, status, actor.user_id, reason, decision)
        self.audit.note(
            assignment["record_id"],
            actor.user_id,
            "assignment_reviewed",
            {
                "assignment_id": assignment_id,
                "decision": decision,
                "reason": reason,
                "status": status,
                "conflicts": updated["conflicts"],
            },
        )
        return self._record_with_assignments(self.repository.get(assignment["record_id"]))

    def recheck_assignment(self, actor: Actor, assignment_id: int) -> Dict[str, Any]:
        """按最新人员资料重新核验（人员关系更新、案件重开后仍能核对）；复核意见保留。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_recheck(actor.role):
            raise PermissionDenied("角色无权重新核验")
        assignment = self.repository.get_assignment(assignment_id)
        if assignment["status"] not in {"active", "pending_review"}:
            raise ValidationError("仅进行中或待复核的指派可以重新核验")
        record = self.repository.get(assignment["record_id"])
        person = self.repository.get_person(assignment["person_id"])
        conflicts = self.rules.check_interest_conflicts(person, record["payload"])
        updated = self.repository.recheck_assignment(assignment_id, conflicts)
        self.audit.note(
            assignment["record_id"],
            actor.user_id,
            "assignment_rechecked",
            {
                "assignment_id": assignment_id,
                "old_status": assignment["status"],
                "new_status": updated["status"],
                "conflicts": conflicts,
                "reviews_kept": len(updated["reviews"]),
            },
        )
        return self._record_with_assignments(record)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        # 存在待复核或已驳回的冲突指派时，案件停在待复核：必须主管写明理由放行，或换人后重新检查
        blocked = self.repository.blocked_assignment_slots(record_id)
        if blocked:
            label = {'pending_review': '待主管复核放行', 'rejected': '已被主管驳回，需要换人'}
            raise Conflict("案件存在未放行的利益冲突指派：" + "；".join(
                "%s %s(%s)：%s" % (a["role_label"], a["person_id"], a["status"], label[a["status"]]) for a in blocked
            ))
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        saved = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._record_with_assignments(saved)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
