"""SQLite 持久化层（会话落盘 + 审计日志）。

对应《产品开发规格书-比赛版v-competition.md》任务 0.5：
"会话落盘（SQLite）+ run_id 审计 —— 验收：重启后能读到历史病例与审核记录"。

设计要点：
- **纯标准库 sqlite3**，不引入 ORM；
- 数据库文件默认 `backend/data/mdt.db`，可用环境变量 `APP_DB_PATH` 覆盖；
  传 `":memory:"` 用于单测；
- 数据库文件已被 `.gitignore`（`*.db`）忽略，**不得提交**；
- 所有写入带 `:memory:` 友好、线程安全（内部加锁）；
- 表：
  - `cases`       病例会话（患者信息、主问题、状态、病例状态、报告版本）
  - `audit_log`   审计日志（每次调用/流转一条，带 run_id）
  - `decisions`   医生审核记录（DoctorDecision）
  - `transitions` 病例状态流转（CaseStateMachine 的时间线，落库可回放）
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

DEFAULT_DB_RELATIVE = os.path.join("data", "mdt.db")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_db_path() -> str:
    """默认数据库路径：backend/data/mdt.db（可用 APP_DB_PATH 覆盖）。"""
    env_path = os.getenv("APP_DB_PATH")
    if env_path:
        return env_path
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(backend_dir, DEFAULT_DB_RELATIVE)


class Store:
    """轻量 SQLite 存储。可安全地跨线程使用（内部串行化）。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or default_db_path()
        if self.db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.db_path))
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------ 建表
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_id        TEXT PRIMARY KEY,
                    owner_id       TEXT NOT NULL DEFAULT '',
                    patient_name   TEXT,
                    main_issue     TEXT,
                    status         TEXT,             -- running / completed / error
                    case_state     TEXT,             -- 规格书 §7.1 状态机
                    report_version INTEGER DEFAULT 1,
                    created_at     TEXT,
                    updated_at     TEXT,
                    payload        TEXT              -- 患者信息等 JSON
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id   TEXT,
                    case_id  TEXT,
                    event    TEXT,
                    detail   TEXT,
                    at       TEXT
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    case_id     TEXT,
                    target_type TEXT,
                    target_id   TEXT,
                    action      TEXT,
                    before      TEXT,
                    after       TEXT,
                    reason      TEXT,
                    reviewer    TEXT,
                    version     INTEGER,
                    created_at  TEXT,
                    payload     TEXT              -- 完整 DoctorDecision JSON
                );

                CREATE TABLE IF NOT EXISTS transitions (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id    TEXT,
                    case_state TEXT,
                    from_state TEXT,
                    to_state   TEXT,
                    actor      TEXT,
                    reason     TEXT,
                    is_anomaly INTEGER DEFAULT 0,
                    at         TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id, at);
                CREATE INDEX IF NOT EXISTS idx_decisions_case ON decisions(case_id, created_at);
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(cases)")}
            if "owner_id" not in columns:
                self._conn.execute("ALTER TABLE cases ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    # ------------------------------------------------------------------ 病例
    def save_case(
        self,
        case_id: str,
        *,
        owner_id: str = "",
        patient_name: str = "",
        main_issue: str = "",
        status: str = "running",
        case_state: str = "pending_materials",
        report_version: int = 1,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cases (case_id, owner_id, patient_name, main_issue, status, case_state,
                                   report_version, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    owner_id      = CASE WHEN excluded.owner_id <> '' THEN excluded.owner_id
                                         ELSE cases.owner_id END,
                    patient_name = excluded.patient_name,
                    main_issue   = excluded.main_issue,
                    status       = excluded.status,
                    case_state   = excluded.case_state,
                    report_version = excluded.report_version,
                    updated_at   = excluded.updated_at,
                    payload      = excluded.payload
                """,
                (case_id, owner_id, patient_name, main_issue, status, case_state,
                 int(report_version), now, now,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )
            self._conn.commit()

    def update_case(
        self,
        case_id: str,
        *,
        status: Optional[str] = None,
        case_state: Optional[str] = None,
        report_version: Optional[int] = None,
    ) -> None:
        sets: List[str] = []
        args: List[Any] = []
        if status is not None:
            sets.append("status = ?"); args.append(status)
        if case_state is not None:
            sets.append("case_state = ?"); args.append(case_state)
        if report_version is not None:
            sets.append("report_version = ?"); args.append(int(report_version))
        if not sets:
            return
        sets.append("updated_at = ?"); args.append(_now())
        args.append(case_id)
        with self._lock:
            self._conn.execute(f"UPDATE cases SET {', '.join(sets)} WHERE case_id = ?", args)
            self._conn.commit()

    def load_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cases WHERE case_id = ?", (case_id,)
            ).fetchone()
        return self._row_to_case(row) if row else None

    def list_cases(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cases ORDER BY created_at DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [self._row_to_case(r) for r in rows]

    def mutate_case(self, case_id: str,
                    mutator: Callable[[Dict[str, Any]], None]) -> Optional[Dict[str, Any]]:
        """在同一把锁内原子地读-改-写病例，防止 JSON payload 并发覆盖。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if not row:
                return None
            case = self._row_to_case(row)
            mutator(case)
            now = _now()
            self._conn.execute(
                """UPDATE cases SET owner_id=?, patient_name=?, main_issue=?, status=?,
                   case_state=?, report_version=?, updated_at=?, payload=? WHERE case_id=?""",
                (case.get("owner_id") or "", case.get("patient_name") or "",
                 case.get("main_issue") or "", case.get("status") or "running",
                 case.get("case_state") or "pending_materials",
                 int(case.get("report_version") or 1), now,
                 json.dumps(case.get("payload") or {}, ensure_ascii=False), case_id),
            )
            self._conn.commit()
            return dict(case)

    def transition_case(self, case_id: str, to_state: str, *, actor: str = "system",
                        reason: str = "", status: Optional[str] = None,
                        report_version: Optional[int] = None,
                        payload: Optional[Dict[str, Any]] = None) -> List[str]:
        """校验并在一个事务中写入病例状态与逐跳流转记录。"""
        from services.case_state import TRANSITIONS, UnknownState, IllegalTransition

        with self._lock:
            row = self._conn.execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
            if not row:
                raise KeyError(f"病例不存在：{case_id}")
            case = self._row_to_case(row)
            start = str(case.get("case_state") or "pending_materials")
            if to_state not in TRANSITIONS:
                raise UnknownState(f"未知状态: {to_state}")

            path: List[str] = []
            if start != to_state:
                queue = [(start, [])]
                seen = {start}
                while queue:
                    current, current_path = queue.pop(0)
                    for nxt in TRANSITIONS.get(current, ()):
                        if nxt == to_state:
                            path = current_path + [nxt]
                            queue = []
                            break
                        if nxt not in seen:
                            seen.add(nxt)
                            queue.append((nxt, current_path + [nxt]))
                    else:
                        continue
                    break
                if not path:
                    raise IllegalTransition(f"无法从 {start} 转移到 {to_state}")

            new_payload = payload if payload is not None else case.get("payload") or {}
            self._conn.execute(
                """UPDATE cases SET status=?, case_state=?, report_version=?, updated_at=?, payload=?
                   WHERE case_id=?""",
                (status if status is not None else case.get("status") or "running", to_state,
                 int(report_version if report_version is not None else case.get("report_version") or 1),
                 _now(), json.dumps(new_payload, ensure_ascii=False), case_id),
            )
            current = start
            for nxt in path:
                self._conn.execute(
                    """INSERT INTO transitions (case_id, case_state, from_state, to_state,
                       actor, reason, is_anomaly, at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
                    (case_id, nxt, current, nxt, actor, reason, _now()),
                )
                current = nxt
            self._conn.commit()
            return path

    @staticmethod
    def _row_to_case(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["payload"] = json.loads(data.get("payload") or "{}")
        except json.JSONDecodeError:
            data["payload"] = {}
        return data

    # ------------------------------------------------------------------ 审计
    def add_audit(self, case_id: str, event: str, detail: str = "",
                  run_id: Optional[str] = None) -> str:
        """写一条审计日志，返回 run_id（未传则新建）。"""
        run_id = run_id or new_run_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (run_id, case_id, event, detail, at) VALUES (?, ?, ?, ?, ?)",
                (run_id, case_id, event, detail, _now()),
            )
            self._conn.commit()
        return run_id

    def list_audit(self, case_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            if case_id:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log WHERE case_id = ? ORDER BY id ASC LIMIT ?",
                    (case_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 医生审核记录
    def save_decision(self, decision: Dict[str, Any]) -> None:
        payload = dict(decision)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO decisions (decision_id, case_id, target_type, target_id, action,
                                       before, after, reason, reviewer, version, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    action = excluded.action, before = excluded.before, after = excluded.after,
                    reason = excluded.reason, reviewer = excluded.reviewer,
                    version = excluded.version, payload = excluded.payload
                """,
                (
                    payload.get("decision_id", ""),
                    payload.get("case_id", ""),
                    payload.get("target_type", ""),
                    payload.get("target_id", ""),
                    payload.get("action", ""),
                    payload.get("before"),
                    payload.get("after"),
                    payload.get("reason", ""),
                    json.dumps(payload.get("reviewer", {}), ensure_ascii=False),
                    int(payload.get("version", 1)),
                    payload.get("created_at") or _now(),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def list_decisions(self, case_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM decisions WHERE case_id = ? ORDER BY created_at ASC",
                (case_id,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except json.JSONDecodeError:
                continue
        return out

    # ------------------------------------------------------------------ 状态流转
    def save_transition(self, case_id: str, from_state: str, to_state: str,
                        actor: str = "system", reason: str = "", is_anomaly: bool = False) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO transitions (case_id, case_state, from_state, to_state,
                                            actor, reason, is_anomaly, at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (case_id, to_state, from_state, to_state, actor, reason,
                 1 if is_anomaly else 0, _now()),
            )
            self._conn.commit()

    def list_transitions(self, case_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transitions WHERE case_id = ? ORDER BY id ASC", (case_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 生命周期
    def close(self) -> None:
        with self._lock:
            self._conn.close()


def new_run_id() -> str:
    """生成一次运行/请求的审计 ID。"""
    return uuid.uuid4().hex[:16]
