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
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

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

    def create_guest_session(self, ttl_minutes: Optional[int] = None) -> Session:
        """Create a short-lived anonymous session with no persistent-case privileges."""
        configured_ttl = int(os.getenv("APP_GUEST_TTL_MINUTES", "30"))
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
    ) -> Session:
        """登录。

        - 传 `username` + `password`：按内置账号校验；
        - 只传 `role`：始终拒绝；免登录体验必须使用受限游客会话。
        """
        if username:
            account = self.accounts.get(username.strip())
            if not account:
                raise AuthError("账号不存在，请核对账号后重试")
            configured_hash = account.get("password_hash") or ""
            configured_password = account.get("password") or ""
            if not configured_hash and not configured_password:
                raise AuthError("该账号尚未配置密码，请联系管理员")
            valid = (verify_password(password or "", configured_hash) if configured_hash
                     else hmac.compare_digest(configured_password, password or ""))
            if not valid:
                raise AuthError("账号或密码不正确")
            return self._create_session(username.strip(), account["role"], account["label"])

        if role:
            raise AuthError("免密角色登录已下线；请使用账号密码或受限游客体验")

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
            self._sessions[session.token] = session
        return session

    # ------------------------------------------------------------------ 校验
    def verify(self, token: Optional[str]) -> Optional[Session]:
        """校验 token；过期或不存在返回 None。"""
        if not token:
            return None
        with self._lock:
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
            return len(self._sessions)

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
