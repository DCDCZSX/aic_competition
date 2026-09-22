"""登录、角色分流与会话守卫（规格书 §3.1/§3.2，主文档 §11）。

产品定位（写死）：
- **账号体系**：内置「就诊者」与「审核医生」两类账号；本地部署、不接第三方注册，
  真实上线时把这里替换为机构统一身份认证；
- **角色分流**：就诊者进患者端，医生进医生工作台；后台按角色鉴权，不能只做前端跳转；
- **未登录不得访问病例接口**：正式病例 API 始终强制校验；
- **游客与正式数据隔离**：免登录体验只能访问独立、限额的 guest API；
- 会话为内存实现，带过期时间；多实例部署时应换成集中式会话存储。

用法：
    auth = AuthService()
    session = auth.login(username="doctor", password=os.environ["APP_DOCTOR_PASSWORD"])
    auth.verify(token)                            # -> Session | None
"""

from __future__ import annotations

import os
import hmac
import hashlib
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from services.config import read_int_env

ROLE_PATIENT = "patient"
ROLE_DOCTOR = "doctor"
ROLE_GUEST = "guest"
VALID_ROLES = (ROLE_PATIENT, ROLE_DOCTOR)

# 角色可访问的能力范围（后台鉴权依据，而非前端约定）
ROLE_SCOPES: Dict[str, List[str]] = {
    ROLE_GUEST: ["guest:preview", "guest:triage"],
    ROLE_PATIENT: ["intake", "case:own", "task:own", "report:own", "update:own"],
    ROLE_DOCTOR: ["review", "case:authorized", "decision", "report:confirm"],
}

def _configured_accounts() -> Dict[str, Dict[str, str]]:
    """从环境变量构建本地账号。

    密码不再写在代码里；缺少密码时账号不可登录。真实部署仍应替换为
    机构身份认证，而不是继续扩展这个本地账号表。
    """
    reviewer_label = ("授权审核医生" if os.getenv("APP_REVIEWER_MODE", "simulation").lower()
                      == "clinical" else "模拟审核员")
    return {
        os.getenv("APP_PATIENT_USERNAME", "patient").strip(): {
            "password": os.getenv("APP_PATIENT_PASSWORD", ""),
            "password_hash": os.getenv("APP_PATIENT_PASSWORD_HASH", ""),
            "role": ROLE_PATIENT,
            "label": "就诊者",
        },
        os.getenv("APP_DOCTOR_USERNAME", "doctor").strip(): {
            "password": os.getenv("APP_DOCTOR_PASSWORD", ""),
            "password_hash": os.getenv("APP_DOCTOR_PASSWORD_HASH", ""),
            "role": ROLE_DOCTOR,
            "label": reviewer_label,
        },
    }

@dataclass
class Session:
    """一次登录会话。"""

    token: str
    username: str
    role: str
    label: str = ""
    created_at: str = ""
    expires_at: str = ""
    usage: Dict[str, int] = field(default_factory=dict, repr=False)

    def to_dict(self, include_token: bool = True) -> Dict[str, str]:
        data = {"username": self.username, "role": self.role, "label": self.label}
        if include_token:
            data["token"] = self.token
        return data

    @property
    def scopes(self) -> List[str]:
        return list(ROLE_SCOPES.get(self.role, []))


def auth_required() -> bool:
    """Full APIs always require authentication; anonymous access uses guest endpoints."""
    return True


def role_login_allowed() -> bool:
    """Legacy passwordless role login is permanently disabled."""
    return False


class AuthError(ValueError):
    """登录失败。"""


class AuthRateLimitError(AuthError):
    """登录冷却中的限流失败。"""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__("账号或密码不正确，请稍后重试")


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    """Return a portable PBKDF2-SHA256 value suitable for environment configuration."""
    if not password:
        raise ValueError("密码不能为空")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
        return hmac.compare_digest(actual.hex(), expected)
    except (TypeError, ValueError):
        return False


class AuthService:
    """内存会话服务（比赛版）。"""

    def __init__(self, ttl_hours: int = 12,
                 accounts: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self.ttl = timedelta(hours=ttl_hours)
        self.accounts = dict(accounts if accounts is not None else _configured_accounts())
        self._login_failures: Dict[str, deque] = {}
        self._max_login_failures = read_int_env(
            "APP_LOGIN_MAX_FAILURES", 5, minimum=1, maximum=100
        )
        self._max_login_failure_keys = read_int_env(
            "APP_MAX_LOGIN_FAILURE_KEYS", 10000, minimum=100, maximum=100000
        )
        self._login_window_seconds = read_int_env(
            "APP_LOGIN_FAILURE_WINDOW_SECONDS", 300,
            minimum=30, maximum=86400
        )
        self._login_cooldown_seconds = read_int_env(
            "APP_LOGIN_COOLDOWN_SECONDS", 60,
            minimum=5, maximum=3600
        )
        self._max_sessions_per_account = read_int_env(
            "APP_MAX_SESSIONS_PER_ACCOUNT", 5,
            minimum=1, maximum=1000
        )
        self._max_total_sessions = read_int_env(
            "APP_MAX_TOTAL_SESSIONS", 10000,
            minimum=100, maximum=1000000
        )

    def create_guest_session(self, ttl_minutes: Optional[int] = None) -> Session:
        """Create a short-lived anonymous session with no persistent-case privileges."""
        configured_ttl = read_int_env(
            "APP_GUEST_TTL_MINUTES", 30, minimum=5, maximum=120
        )
        minutes = max(5, min(ttl_minutes or configured_ttl, 120))
        guest_id = f"guest-{secrets.token_hex(8)}"
        return self._create_session(
            guest_id, ROLE_GUEST, "游客体验", ttl=timedelta(minutes=minutes)
        )

    # ------------------------------------------------------------------ 登录
    def login(
        self,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        role: Optional[str] = None,
        client_key: Optional[str] = None,
    ) -> Session:
        """登录。

        - 传 `username` + `password`：按内置账号校验；
        - 只传 `role`：始终拒绝；免登录体验必须使用受限游客会话。
        """
        # 账号名/密码类型错误也走同一模糊错误，避免异常输入触发 500 或暴露账号信息。
        normalized_username = username.strip() if isinstance(username, str) else ""
        identity_keys = self._login_keys(normalized_username, client_key)
        retry_after = self._login_retry_after(identity_keys)
        if retry_after is not None:
            raise AuthRateLimitError(retry_after)

        if normalized_username:
            account = self.accounts.get(normalized_username)
            if not account:
                self._record_login_failure(identity_keys)
                raise AuthError("账号或密码不正确，请稍后重试")
            configured_hash = account.get("password_hash") or ""
            configured_password = account.get("password") or ""
            if not configured_hash and not configured_password:
                self._record_login_failure(identity_keys)
                raise AuthError("账号或密码不正确，请稍后重试")
            if password is not None and not isinstance(password, str):
                self._record_login_failure(identity_keys)
                raise AuthError("账号或密码不正确，请稍后重试")
            valid = (verify_password(password or "", configured_hash) if configured_hash
                     else hmac.compare_digest(configured_password, password or ""))
            if not valid:
                self._record_login_failure(identity_keys)
                raise AuthError("账号或密码不正确，请稍后重试")
            self._clear_login_failures(identity_keys)
            return self._create_session(normalized_username, account["role"], account["label"])

        if role:
            self._record_login_failure(identity_keys)
            raise AuthError("免密角色登录已下线；请使用账号密码或受限游客体验")

        self._record_login_failure(identity_keys)
        raise AuthError("请提供账号密码，或选择进入患者端 / 医生工作台")

    def _create_session(self, username: str, role: str, label: str,
                        ttl: Optional[timedelta] = None) -> Session:
        now = datetime.now()
        session_ttl = ttl or self.ttl
        session = Session(
            token=secrets.token_urlsafe(24),
            username=username,
            role=role,
            label=label,
            created_at=now.isoformat(timespec="seconds"),
            expires_at=(now + session_ttl).isoformat(timespec="seconds"),
        )
        with self._lock:
            self._prune_sessions_locked()
            if role != ROLE_GUEST:
                active_for_account = sum(
                    1 for item in self._sessions.values()
                    if item.username == username and item.role == role
                )
                if active_for_account >= self._max_sessions_per_account:
                    raise AuthError("当前账号活跃会话过多，请先退出其他会话")
            elif len(self._sessions) >= self._max_total_sessions:
                raise AuthError("当前游客服务繁忙，请稍后重试")
            self._sessions[session.token] = session
        return session

    # ------------------------------------------------------------------ 校验
    def verify(self, token: Optional[str]) -> Optional[Session]:
        """校验 token；过期或不存在返回 None。"""
        if not token:
            return None
        with self._lock:
            self._prune_sessions_locked()
            session = self._sessions.get(token)
            if not session:
                return None
            if self._expired(session):
                self._sessions.pop(token, None)
                return None
            return session

    def logout(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def has_scope(self, session: Optional[Session], scope: str) -> bool:
        """角色能力校验（后台鉴权）。"""
        return bool(session) and scope in session.scopes

    def is_reviewer_account(self, username: str) -> bool:
        return bool(username) and (self.accounts.get(username) or {}).get("role") == ROLE_DOCTOR

    def consume(self, session: Optional[Session], capability: str, limit: int) -> bool:
        """Atomically consume one per-session allowance without exposing counters to clients."""
        if not session or limit < 1:
            return False
        with self._lock:
            current = self._sessions.get(session.token)
            if current is None or self._expired(current):
                self._sessions.pop(session.token, None)
                return False
            used = int(current.usage.get(capability, 0))
            if used >= limit:
                return False
            current.usage[capability] = used + 1
            return True

    def active_count(self) -> int:
        with self._lock:
            self._prune_sessions_locked()
            return len(self._sessions)

    @staticmethod
    def _login_keys(username: str, client_key: Optional[str]) -> List[str]:
        """按账号和来源地址分别限流；账号名使用不可逆摘要，避免保留明文。"""
        keys = []
        if username:
            keys.append("user:" + hashlib.sha256(username.lower().encode()).hexdigest())
        if client_key:
            raw = str(client_key).strip()[:128]
            if raw:
                keys.append("ip:" + hashlib.sha256(raw.encode()).hexdigest())
        return keys or ["unknown"]

    def _login_retry_after(self, keys: List[str]) -> Optional[int]:
        now = time.time()
        with self._lock:
            retry_after = None
            for key in keys:
                attempts = self._login_failures.get(key)
                if not attempts:
                    continue
                while attempts and attempts[0] <= now - self._login_window_seconds:
                    attempts.popleft()
                if attempts and (
                    len(attempts) >= self._max_login_failures
                    and now - attempts[-1] < self._login_cooldown_seconds
                ):
                    remaining = math.ceil(self._login_cooldown_seconds - (now - attempts[-1]))
                    retry_after = max(retry_after or 0, remaining)
                if not attempts:
                    self._login_failures.pop(key, None)
        return retry_after

    def _record_login_failure(self, keys: List[str]) -> None:
        now = time.time()
        with self._lock:
            # 先清理过期桶，再限制不同账号/IP 键的总量，避免随机用户名长期撑大内存。
            for stored_key, attempts in list(self._login_failures.items()):
                while attempts and attempts[0] <= now - self._login_window_seconds:
                    attempts.popleft()
                if not attempts:
                    self._login_failures.pop(stored_key, None)
            for key in keys:
                if (key not in self._login_failures
                        and len(self._login_failures) >= self._max_login_failure_keys):
                    oldest_key = min(
                        self._login_failures,
                        key=lambda item: self._login_failures[item][-1],
                    )
                    self._login_failures.pop(oldest_key, None)
                attempts = self._login_failures.setdefault(key, deque())
                while attempts and attempts[0] <= now - self._login_window_seconds:
                    attempts.popleft()
                attempts.append(now)

    def _clear_login_failures(self, keys: List[str]) -> None:
        with self._lock:
            for key in keys:
                self._login_failures.pop(key, None)

    def _prune_sessions_locked(self) -> None:
        expired = [token for token, session in self._sessions.items() if self._expired(session)]
        for token in expired:
            self._sessions.pop(token, None)

    def _expired(self, session: Session) -> bool:
        try:
            return datetime.fromisoformat(session.expires_at) < datetime.now()
        except ValueError:
            return True


def extract_token(header_value: Optional[str]) -> Optional[str]:
    """只从 `Authorization: Bearer xxx` 取 token，避免 token 进入 URL/日志/历史记录。"""
    if header_value:
        parts = header_value.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None
