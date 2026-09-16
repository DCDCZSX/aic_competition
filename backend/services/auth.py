"""登录、角色分流与会话守卫（规格书 §3.1/§3.2，主文档 §11）。

产品定位（写死）：
- **账号体系**：内置「就诊者」与「审核医生」两类账号；本地部署、不接第三方注册，
  真实上线时把这里替换为机构统一身份认证；
- **角色分流**：就诊者进患者端，医生进医生工作台；后台按角色鉴权，不能只做前端跳转；
- **未登录不得访问病例接口**：`APP_AUTH_REQUIRED=true` 时强制校验（生产部署请开启）；
- 会话为内存实现，带过期时间；多实例部署时应换成集中式会话存储。

用法：
    auth = AuthService()
    session = auth.login(username="doctor", password=os.environ["APP_DOCTOR_PASSWORD"])
    auth.verify(token)                            # -> Session | None
"""

from __future__ import annotations

import os
import hmac
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

ROLE_PATIENT = "patient"
ROLE_DOCTOR = "doctor"
VALID_ROLES = (ROLE_PATIENT, ROLE_DOCTOR)

# 角色可访问的能力范围（后台鉴权依据，而非前端约定）
ROLE_SCOPES: Dict[str, List[str]] = {
    ROLE_PATIENT: ["intake", "case:own", "task:own", "report:own", "update:own"],
    ROLE_DOCTOR: ["review", "case:authorized", "decision", "report:confirm"],
}

def _configured_accounts() -> Dict[str, Dict[str, str]]:
    """从环境变量构建本地账号。

    密码不再写在代码里；缺少密码时账号不可登录。真实部署仍应替换为
    机构身份认证，而不是继续扩展这个本地账号表。
    """
    return {
        os.getenv("APP_PATIENT_USERNAME", "patient").strip(): {
            "password": os.getenv("APP_PATIENT_PASSWORD", ""),
            "role": ROLE_PATIENT,
            "label": "就诊者",
        },
        os.getenv("APP_DOCTOR_USERNAME", "doctor").strip(): {
            "password": os.getenv("APP_DOCTOR_PASSWORD", ""),
            "role": ROLE_DOCTOR,
            "label": "审核医生",
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

    def to_dict(self, include_token: bool = True) -> Dict[str, str]:
        data = {"username": self.username, "role": self.role, "label": self.label}
        if include_token:
            data["token"] = self.token
        return data

    @property
    def scopes(self) -> List[str]:
        return list(ROLE_SCOPES.get(self.role, []))


def auth_required() -> bool:
    """是否强制登录。默认开启；仅显式设为 false 时用于隔离的本地测试。"""
    return os.getenv("APP_AUTH_REQUIRED", "true").strip().lower() not in ("0", "false", "no")


def role_login_allowed() -> bool:
    """是否允许免密角色入口。默认禁用，仅可在无真实数据的本地演示中显式开启。"""
    return os.getenv("APP_ALLOW_ROLE_LOGIN", "false").strip().lower() in ("1", "true", "yes")


class AuthError(ValueError):
    """登录失败。"""


class AuthService:
    """内存会话服务（比赛版）。"""

    def __init__(self, ttl_hours: int = 12,
                 accounts: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self.ttl = timedelta(hours=ttl_hours)
        self.accounts = dict(accounts if accounts is not None else _configured_accounts())

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
        - 只传 `role`：仅当 `APP_ALLOW_ROLE_LOGIN=true` 时允许，用于无真实数据的隔离演示。
        """
        if username:
            account = self.accounts.get(username.strip())
            if not account:
                raise AuthError("账号不存在，请核对账号后重试")
            configured_password = account.get("password") or ""
            if not configured_password:
                raise AuthError("该账号尚未配置密码，请联系管理员")
            if not hmac.compare_digest(configured_password, password or ""):
                raise AuthError("账号或密码不正确")
            return self._create_session(username.strip(), account["role"], account["label"])

        if role:
            if not role_login_allowed():
                raise AuthError("已禁用免密角色登录，请使用账号和密码")
            if role not in VALID_ROLES:
                raise AuthError(f"未知角色：{role}（可选：{' / '.join(VALID_ROLES)}）")
            username = next((name for name, item in self.accounts.items()
                             if item.get("role") == role), "")
            account = self.accounts[username]
            return self._create_session(username, account["role"], account["label"])

        raise AuthError("请提供账号密码，或选择进入患者端 / 医生工作台")

    def _create_session(self, username: str, role: str, label: str) -> Session:
        now = datetime.now()
        session = Session(
            token=secrets.token_urlsafe(24),
            username=username,
            role=role,
            label=label,
            created_at=now.isoformat(timespec="seconds"),
            expires_at=(now + self.ttl).isoformat(timespec="seconds"),
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
