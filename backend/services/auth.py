"""登录、角色分流与会话守卫（规格书 §3.1/§3.2，主文档 §11）。

产品定位（写死）：
- **账号体系**：内置「就诊者」与「审核医生」两类账号；本地部署、不接第三方注册，
  真实上线时把这里替换为机构统一身份认证；
- **角色分流**：就诊者进患者端，医生进医生工作台；后台按角色鉴权，不能只做前端跳转；
- **未登录不得访问病例接口**：`APP_AUTH_REQUIRED=true` 时强制校验（生产部署请开启）；
- 会话为内存实现，带过期时间；多实例部署时应换成集中式会话存储。

用法：
    auth = AuthService()
    session = auth.login(username="doctor", password="doctor")
    session = auth.login(role="patient")          # 角色入口（等价于该角色账号）
    auth.verify(token)                            # -> Session | None
"""

from __future__ import annotations

import os
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

# 内置账号（本机部署的可用账号；真实上线接机构认证并强制改密）
ACCOUNTS: Dict[str, Dict[str, str]] = {
    "patient": {"password": "patient", "role": ROLE_PATIENT, "label": "就诊者"},
    "doctor": {"password": "doctor", "role": ROLE_DOCTOR, "label": "审核医生"},
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
    """是否强制登录（环境变量 APP_AUTH_REQUIRED，默认 false 以兼容本地开发）。"""
    return os.getenv("APP_AUTH_REQUIRED", "false").strip().lower() in ("1", "true", "yes")


class AuthError(ValueError):
    """登录失败。"""


class AuthService:
    """内存会话服务（比赛版）。"""

    def __init__(self, ttl_hours: int = 12) -> None:
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()
        self.ttl = timedelta(hours=ttl_hours)

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
        - 只传 `role`：角色入口（等价于该角色账号，供页面上的角色卡直接进入）。
        """
        if username:
            account = ACCOUNTS.get(username.strip())
            if not account:
                raise AuthError("账号不存在，请核对账号后重试")
            if account["password"] != (password or ""):
                raise AuthError("账号或密码不正确")
            return self._create_session(username.strip(), account["role"], account["label"])

        if role:
            if role not in VALID_ROLES:
                raise AuthError(f"未知角色：{role}（可选：{' / '.join(VALID_ROLES)}）")
            username = "patient" if role == ROLE_PATIENT else "doctor"
            account = ACCOUNTS[username]
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


def extract_token(header_value: Optional[str], fallback: Optional[str] = None) -> Optional[str]:
    """从 `Authorization: Bearer xxx` 或查询参数中取出 token。"""
    if header_value:
        parts = header_value.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return header_value.strip()
    return fallback
