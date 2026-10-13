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
        # 本进程已分配但可能尚未落库的病例号（建档前的画像抽取可能耗时），防止并发重号。
        self._reserved_case_ids: set = set()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 多线程后台分析与 HTTP 请求会并发访问 SQLite；显式设置忙等待，
        # 文件库启用 WAL 降低读写互相阻塞。内存库不设置 WAL，保持测试兼容。
        self._conn.execute("PRAGMA busy_timeout=5000")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
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
                    payload        TEXT,             -- 患者信息等 JSON
                    archived_at    TEXT              -- 患者主动归档时间；归档可恢复
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

                CREATE TABLE IF NOT EXISTS report_versions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id         TEXT NOT NULL,
                    version         INTEGER NOT NULL,
                    published_by    TEXT NOT NULL,
                    published_at    TEXT NOT NULL,
                    review_summary  TEXT NOT NULL,
                    report_json     TEXT NOT NULL,
                    summary_markdown TEXT NOT NULL,
                    full_markdown   TEXT NOT NULL,
                    UNIQUE(case_id, version)
                );

                CREATE TABLE IF NOT EXISTS case_access (
                    case_id    TEXT NOT NULL,
                    reviewer_id TEXT NOT NULL,
                    case_role  TEXT NOT NULL DEFAULT 'primary',
                    granted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (case_id, reviewer_id)
                );

                CREATE TABLE IF NOT EXISTS users (
                    username        TEXT PRIMARY KEY,
                    password_hash   TEXT NOT NULL,
                    role            TEXT NOT NULL,
                    label           TEXT NOT NULL,
                    department_code TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id, at);
                CREATE INDEX IF NOT EXISTS idx_decisions_case ON decisions(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_case_access_reviewer
                    ON case_access(reviewer_id, case_id);
                CREATE INDEX IF NOT EXISTS idx_users_role_status
                    ON users(role, status, username);
                CREATE INDEX IF NOT EXISTS idx_users_department
                    ON users(role, department_code, status, username);
                CREATE INDEX IF NOT EXISTS idx_cases_created
                    ON cases(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_report_versions_case
                    ON report_versions(case_id, version ASC);
                """
            )
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(cases)")}
            if "owner_id" not in columns:
                self._conn.execute("ALTER TABLE cases ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            if "archived_at" not in columns:
                self._conn.execute("ALTER TABLE cases ADD COLUMN archived_at TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cases_owner ON cases(owner_id, created_at DESC)"
            )
            access_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(case_access)")}
            if "case_role" not in access_columns:
                self._conn.execute(
                    "ALTER TABLE case_access ADD COLUMN case_role TEXT NOT NULL DEFAULT 'primary'"
                )
            self._conn.commit()

    # ------------------------------------------------------------------ 病例
    def next_case_id(self, prefix: str = "YX", day: Optional[str] = None) -> str:
        """分配按日期编号的病例号：``{prefix}{YYYYMMDD}-{当日 4 位流水}``。

        在存储锁内取“库内当日最大流水”和“本进程已预留流水”的较大者再加一，
        保证同一进程内并发建档不会拿到相同编号；单进程 demo 边界与会话、令牌一致。
        """
        day = day or datetime.now().strftime("%Y%m%d")
        stem = f"{prefix}{day}-"
        with self._lock:
            rows = self._conn.execute(
                "SELECT case_id FROM cases WHERE case_id LIKE ?", (stem + "%",)
            ).fetchall()
            used = {str(row[0]) for row in rows} | {
                cid for cid in self._reserved_case_ids if cid.startswith(stem)}
            highest = 0
            for cid in used:
                tail = cid[len(stem):]
                if tail.isdigit():
                    highest = max(highest, int(tail))
            case_id = f"{stem}{highest + 1:04d}"
            self._reserved_case_ids.add(case_id)
            return case_id

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

    def list_cases(self, limit: Optional[int] = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """按创建时间倒序取病例窗口。

        ``offset`` 只负责持久化层的窗口读取；医生端队列的风险排序仍由
        ``CaseFlow.list_review_cases`` 在计算审核摘要后完成。
        """
        safe_offset = max(0, int(offset))
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    "SELECT * FROM cases ORDER BY created_at DESC"
                ).fetchall()
            else:
                safe_limit = max(0, int(limit))
                rows = self._conn.execute(
                    "SELECT * FROM cases ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (safe_limit, safe_offset),
                ).fetchall()
        return [self._row_to_case(r) for r in rows]

    def list_cases_for_owner(self, owner_id: str, *, limit: Optional[int] = None,
                             offset: int = 0) -> List[Dict[str, Any]]:
        """Return only cases owned by one patient, newest first."""
        if not owner_id:
            return []
        with self._lock:
            query = "SELECT * FROM cases WHERE owner_id=? AND archived_at IS NULL ORDER BY updated_at DESC, created_at DESC"
            args: List[Any] = [owner_id]
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                args.extend([max(0, int(limit)), max(0, int(offset))])
            rows = self._conn.execute(query, args).fetchall()
        return [self._row_to_case(r) for r in rows]

    def count_cases_for_owner(self, owner_id: str) -> int:
        if not owner_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM cases WHERE owner_id=? AND archived_at IS NULL", (owner_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def archive_case_for_owner(self, case_id: str, owner_id: str) -> bool:
        if not case_id or not owner_id:
            return False
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cases SET archived_at=?, updated_at=? "
                "WHERE case_id=? AND owner_id=? AND archived_at IS NULL",
                (now, now, case_id, owner_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def restore_case_for_owner(self, case_id: str, owner_id: str) -> bool:
        if not case_id or not owner_id:
            return False
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cases SET archived_at=NULL, updated_at=? "
                "WHERE case_id=? AND owner_id=? AND archived_at IS NOT NULL",
                (_now(), case_id, owner_id),
            )
            self._conn.commit()
        return cur.rowcount == 1

    def list_cases_for_reviewer(self, reviewer_id: str, *, limit: Optional[int] = None,
                                offset: int = 0) -> List[Dict[str, Any]]:
        """只读取已明确授权给审核员的病例。"""
        if not reviewer_id:
            return self.list_cases(limit=limit, offset=offset)
        safe_offset = max(0, int(offset))
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    """SELECT c.* FROM cases c
                       INNER JOIN case_access a ON a.case_id = c.case_id
                       WHERE a.reviewer_id = ? ORDER BY c.created_at DESC""",
                    (reviewer_id,),
                ).fetchall()
            else:
                safe_limit = max(0, int(limit))
                rows = self._conn.execute(
                    """SELECT c.* FROM cases c
                       INNER JOIN case_access a ON a.case_id = c.case_id
                       WHERE a.reviewer_id = ? ORDER BY c.created_at DESC
                       LIMIT ? OFFSET ?""",
                    (reviewer_id, safe_limit, safe_offset),
                ).fetchall()
        return [self._row_to_case(r) for r in rows]

    def grant_case_access(self, case_id: str, reviewer_id: str, *, granted_by: str,
                          case_role: str = "primary") -> bool:
        if not case_id or not reviewer_id or not self.load_case(case_id):
            return False
        if case_role not in {"primary", "collaborator", "coordinator"}:
            raise ValueError("病例职责不受支持")
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM case_access WHERE case_id=? AND reviewer_id=?",
                (case_id, reviewer_id),
            ).fetchone()
            if existing:
                return False
            self._conn.execute(
                """INSERT INTO case_access (case_id, reviewer_id, case_role, granted_by, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   """,
                (case_id, reviewer_id, case_role, granted_by, _now()),
            )
            self._conn.commit()
        self.add_audit(case_id, "case_access_granted",
                       f"reviewer={reviewer_id}; granted_by={granted_by}")
        return True

    def get_case_access_role(self, case_id: str, reviewer_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT case_role FROM case_access WHERE case_id=? AND reviewer_id=?",
                (case_id, reviewer_id),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def assign_case_role(self, case_id: str, reviewer_id: str, *, case_role: str,
                         actor: str, reason: str) -> bool:
        """Explicit business assignment; sharing alone never calls this method."""
        if case_role not in {"primary", "collaborator", "coordinator"}:
            raise ValueError("病例职责不受支持")
        with self._lock:
            previous = self.get_case_access_role(case_id, reviewer_id)
            if previous is None:
                return self.grant_case_access(case_id, reviewer_id, granted_by=actor, case_role=case_role)
            if previous == case_role:
                return False
            self._conn.execute("UPDATE case_access SET case_role=? WHERE case_id=? AND reviewer_id=?",
                               (case_role, case_id, reviewer_id))
            self._conn.commit()
            self.add_audit(case_id, "case_role_changed",
                           f"reviewer={reviewer_id}; {previous} → {case_role}; actor={actor}; reason={reason}")
            return True

    def revoke_case_access(self, case_id: str, reviewer_id: str, *, revoked_by: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM case_access WHERE case_id=? AND reviewer_id=?",
                (case_id, reviewer_id),
            )
            self._conn.commit()
        removed = cursor.rowcount > 0
        if removed:
            self.add_audit(case_id, "case_access_revoked",
                           f"reviewer={reviewer_id}; revoked_by={revoked_by}")
        return removed

    def has_case_access(self, case_id: str, reviewer_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM case_access WHERE case_id=? AND reviewer_id=?",
                (case_id, reviewer_id),
            ).fetchone()
        return row is not None

    def list_authorized_case_ids(self, reviewer_id: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT case_id FROM case_access WHERE reviewer_id=? ORDER BY created_at DESC",
                (reviewer_id,),
            ).fetchall()
        return [str(row[0]) for row in rows]

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
                        payload: Optional[Dict[str, Any]] = None,
                        report_version_snapshot: Optional[Dict[str, Any]] = None) -> List[str]:
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
            try:
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
                if report_version_snapshot is not None:
                    self._insert_report_version_locked(report_version_snapshot)
                self._conn.commit()
                return path
            except Exception:
                self._conn.rollback()
                raise

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
                    "SELECT * FROM audit_log WHERE case_id = ? ORDER BY id DESC LIMIT ?",
                    (case_id, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
        records = [dict(r) for r in rows]
        return list(reversed(records)) if case_id else records

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
                "SELECT payload FROM decisions WHERE case_id = ? "
                "ORDER BY created_at ASC, rowid ASC",
                (case_id,),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                out.append(json.loads(r["payload"]))
            except json.JSONDecodeError:
                continue
        return out

    def list_decisions_for_cases(self, case_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """批量读取审核决策，避免医生队列的 N+1 查询。"""
        ids = [str(case_id) for case_id in case_ids if str(case_id)]
        if not ids:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {case_id: [] for case_id in ids}
        # SQLite 默认变量上限常见为 999；分块避免大队列一次查询越界。
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT case_id, payload FROM decisions WHERE case_id IN ({placeholders}) "
                    "ORDER BY created_at ASC, rowid ASC",
                    chunk,
                ).fetchall()
            for row in rows:
                try:
                    out.setdefault(str(row["case_id"]), []).append(json.loads(row["payload"]))
                except json.JSONDecodeError:
                    continue
        return out

    # ------------------------------------------------------------------ 不可变报告版本
    @staticmethod
    def _report_version_payload(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        required = ("case_id", "version", "published_by", "published_at",
                    "review_summary", "report_json", "summary_markdown", "full_markdown")
        missing = [key for key in required if key not in snapshot]
        if missing:
            raise ValueError("报告版本缺少字段：" + "、".join(missing))
        payload = dict(snapshot)
        payload["case_id"] = str(payload["case_id"])
        payload["version"] = int(payload["version"])
        payload["published_by"] = str(payload["published_by"])
        payload["published_at"] = str(payload["published_at"])
        payload["review_summary"] = payload["review_summary"] or {}
        payload["report_json"] = payload["report_json"] or {}
        payload["summary_markdown"] = str(payload["summary_markdown"] or "")
        payload["full_markdown"] = str(payload["full_markdown"] or "")
        return payload

    def _insert_report_version_locked(self, snapshot: Dict[str, Any]) -> None:
        payload = self._report_version_payload(snapshot)
        try:
            self._conn.execute(
                """INSERT INTO report_versions
                   (case_id, version, published_by, published_at, review_summary,
                    report_json, summary_markdown, full_markdown)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload["case_id"], payload["version"], payload["published_by"],
                 payload["published_at"],
                 json.dumps(payload["review_summary"], ensure_ascii=False),
                 json.dumps(payload["report_json"], ensure_ascii=False),
                 payload["summary_markdown"], payload["full_markdown"]),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("报告版本已存在且不可覆盖") from exc

    def save_report_version(self, snapshot: Dict[str, Any]) -> None:
        """保存一个不可变报告快照；相同病例/版本不会被更新。"""
        with self._lock:
            self._insert_report_version_locked(snapshot)
            self._conn.commit()

    # ------------------------------------------------------------------ 用户
    def create_user(
        self,
        username: str,
        password_hash: str,
        *,
        role: str,
        label: str,
        department_code: str = "",
        status: str = "active",
    ) -> bool:
        """插入一个用户；用户名已存在时不覆盖原账号。"""
        now = _now()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, role, label, department_code,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (username, password_hash, role, label, department_code, status, now, now),
                )
            except sqlite3.IntegrityError:
                return False
            self._conn.commit()
        return True

    def load_user(self, username: str) -> Optional[Dict[str, Any]]:
        """读取账号内部记录（含哈希，仅供认证服务使用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def list_users(
        self,
        *,
        role: Optional[str] = None,
        department_code: Optional[str] = None,
        status: str = "active",
    ) -> List[Dict[str, Any]]:
        """按角色、科室和状态读取用户，默认只返回 active 用户。"""
        clauses = []
        args: List[Any] = []
        if role is not None:
            clauses.append("role = ?")
            args.append(role)
        if department_code is not None:
            clauses.append("department_code = ?")
            args.append(department_code)
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM users{where} ORDER BY username ASC", args
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _row_to_report_version(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("review_summary", "report_json"):
            try:
                data[key] = json.loads(data.get(key) or "{}")
            except json.JSONDecodeError:
                data[key] = {}
        # 便于调用方直接读取摘要/完整报告，同时保留原始组合 JSON 字段。
        report_json = data.get("report_json") or {}
        if isinstance(report_json, dict):
            data.setdefault("summary", report_json.get("summary"))
            data.setdefault("full", report_json.get("full"))
        return data

    def list_report_versions(self, case_id: str, *, include_reports: bool = True) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM report_versions WHERE case_id=? ORDER BY version ASC",
                (case_id,),
            ).fetchall()
        versions = [self._row_to_report_version(row) for row in rows]
        if not include_reports:
            for version in versions:
                version.pop("report_json", None)
                version.pop("summary", None)
                version.pop("full", None)
                version.pop("summary_markdown", None)
                version.pop("full_markdown", None)
        return versions

    def get_report_version(self, case_id: str, version: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM report_versions WHERE case_id=? AND version=?",
                (case_id, int(version)),
            ).fetchone()
        return self._row_to_report_version(row) if row else None

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
