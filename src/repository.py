"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import ASSIGNMENT_ROLE_LABELS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS personnel (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS person_relatives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL REFERENCES personnel(person_id) ON DELETE CASCADE,
                    other_party_id TEXT NOT NULL,
                    other_party_kind TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    UNIQUE(person_id, other_party_kind, other_party_id)
                );
                CREATE TABLE IF NOT EXISTS person_companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    person_id TEXT NOT NULL REFERENCES personnel(person_id) ON DELETE CASCADE,
                    company_id TEXT NOT NULL,
                    company_name TEXT NOT NULL DEFAULT '',
                    relation TEXT NOT NULL DEFAULT '',
                    UNIQUE(person_id, company_id)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    person_id TEXT NOT NULL,
                    person_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    conflicts TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    superseded_by INTEGER,
                    reviews TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT NOT NULL,
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    reviewed_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_assignments_record ON assignments(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_person_relatives_party ON person_relatives(other_party_kind, other_party_id);
                CREATE INDEX IF NOT EXISTS idx_person_companies_company ON person_companies(company_id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _assignment_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["conflicts"] = json.loads(item["conflicts"])
        item["reviews"] = json.loads(item["reviews"])
        latest = item["reviews"][-1] if item["reviews"] else None
        item["latest_review"] = latest
        item["has_conflict"] = bool(item["conflicts"])
        item["role_label"] = ASSIGNMENT_ROLE_LABELS.get(item["role"], item["role"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 人员资料：基本信息、亲属关系、共同公司 ----

    def upsert_person(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM personnel WHERE person_id=?", (data["person_id"],)).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO personnel(person_id,name,title,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (data["person_id"], data["name"], data["title"], actor_id, now, now),
                )
            else:
                connection.execute(
                    "UPDATE personnel SET name=?,title=?,updated_by=?,updated_at=? WHERE person_id=?",
                    (data["name"], data["title"], actor_id, now, data["person_id"]),
                )
            connection.execute("DELETE FROM person_relatives WHERE person_id=?", (data["person_id"],))
            connection.execute("DELETE FROM person_companies WHERE person_id=?", (data["person_id"],))
            for rel in data["relatives"]:
                connection.execute(
                    "INSERT INTO person_relatives(person_id,other_party_id,other_party_kind,relation) VALUES(?,?,?,?)",
                    (data["person_id"], rel["other_party_id"], rel["other_party_kind"], rel["relation"]),
                )
            for company in data["companies"]:
                connection.execute(
                    "INSERT INTO person_companies(person_id,company_id,company_name,relation) VALUES(?,?,?,?)",
                    (data["person_id"], company["company_id"], company["company_name"], company["relation"]),
                )
            connection.commit()
        return self.get_person(data["person_id"])

    def get_person(self, person_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM personnel WHERE person_id=?", (person_id,)).fetchone()
            if row is None:
                raise NotFound("人员不存在")
            relatives = connection.execute(
                "SELECT other_party_id,other_party_kind,relation FROM person_relatives WHERE person_id=?",
                (person_id,),
            ).fetchall()
            companies = connection.execute(
                "SELECT company_id,company_name,relation FROM person_companies WHERE person_id=?",
                (person_id,),
            ).fetchall()
        item = dict(row)
        item["relatives"] = [dict(r) for r in relatives]
        item["companies"] = [dict(c) for c in companies]
        return item

    def list_persons(self, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT person_id,name,title FROM personnel ORDER BY person_id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---- 指派与利益冲突复核 ----

    def insert_assignment(self, record_id: int, role: str, person_id: str, person_name: str, status: str, conflicts: List[Dict[str, Any]], note: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO assignments(record_id,role,person_id,person_name,status,conflicts,note,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, role, person_id, person_name, status, json.dumps(conflicts, ensure_ascii=False, sort_keys=True), note, actor_id, now, now),
            )
            assignment_id = int(cursor.lastrowid)
            connection.commit()
            row = connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        return self._assignment_row(row)

    def get_assignment(self, assignment_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        if row is None:
            raise NotFound("指派不存在")
        return self._assignment_row(row)

    def latest_assignment(self, record_id: int, role: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM assignments WHERE record_id=? AND role=? ORDER BY id DESC LIMIT 1",
                (record_id, role),
            ).fetchone()
        return self._assignment_row(row) if row is not None else None

    def list_assignments(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM assignments WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._assignment_row(row) for row in rows]

    def has_pending_conflict(self, record_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM assignments WHERE record_id=? AND status='pending_review' LIMIT 1",
                (record_id,),
            ).fetchone()
        return row is not None

    def blocked_assignment_slots(self, record_id: int) -> List[Dict[str, Any]]:
        """返回各席位最新一条仍在待复核或已被驳回的指派——案件须放行或换人后才能继续。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.* FROM assignments a
                JOIN (SELECT role, MAX(id) AS max_id FROM assignments WHERE record_id=? GROUP BY role) m
                  ON a.role = m.role AND a.id = m.max_id
                WHERE a.status IN ('pending_review','rejected')
                ORDER BY a.role
                """,
                (record_id,),
            ).fetchall()
        return [self._assignment_row(row) for row in rows]

    def mark_assignment_superseded(self, assignment_id: int, superseded_by: int, note: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE assignments SET status='removed', superseded_by=?, note=?, updated_at=? WHERE id=? AND status IN ('active','pending_review')",
                (superseded_by, note, _now(), assignment_id),
            )
            connection.commit()

    def review_assignment(self, assignment_id: int, status: str, reviewer_id: str, reason: str, decision: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT reviews FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise NotFound("指派不存在")
            reviews = json.loads(row["reviews"])
            reviews.append({"decision": decision, "reason": reason, "reviewer_id": reviewer_id, "reviewed_at": now})
            connection.execute(
                "UPDATE assignments SET status=?, reviews=?, reviewed_by=?, reviewed_at=?, updated_at=? WHERE id=?",
                (status, json.dumps(reviews, ensure_ascii=False, sort_keys=True), reviewer_id, now, now, assignment_id),
            )
            connection.commit()
            updated = connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        return self._assignment_row(updated)

    def recheck_assignment(self, assignment_id: int, conflicts: List[Dict[str, Any]]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            row = connection.execute("SELECT status, reviews FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise NotFound("指派不存在")
            old_status = str(row["status"])
            # active 指派对上新冲突回到待复核；待复核的即使冲突已解除，也必须由主管重新放行
            new_status = "pending_review" if conflicts else ("active" if old_status == "active" else "pending_review")
            connection.execute(
                "UPDATE assignments SET status=?, conflicts=?, updated_at=? WHERE id=?",
                (new_status, json.dumps(conflicts, ensure_ascii=False, sort_keys=True), now, assignment_id),
            )
            updated = connection.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            connection.commit()
        return self._assignment_row(updated)
