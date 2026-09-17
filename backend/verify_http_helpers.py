"""Shared authentication and privacy-preview helpers for live HTTP checks."""

import os
from typing import Dict

import requests


def login(session: requests.Session, base: str, role: str) -> Dict[str, str]:
    prefix = "APP_DOCTOR" if role == "doctor" else "APP_PATIENT"
    username = os.getenv(f"{prefix}_USERNAME", role).strip()
    password = os.getenv(f"{prefix}_PASSWORD", "")
    if not password:
        raise RuntimeError(f"未配置 {prefix}_PASSWORD，无法运行需登录的 HTTP 验证")
    response = session.post(
        f"{base}/api/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{role} 登录返回非 JSON（HTTP {response.status_code}）") from exc
    token = ((body.get("session") or {}).get("token"))
    if response.status_code != 200 or not token:
        raise RuntimeError(f"{role} 登录失败（HTTP {response.status_code}）：{body.get('error', '未知错误')}")
    session.headers["Authorization"] = f"Bearer {token}"
    return body


def preview_token(session: requests.Session, base: str,
                  medical_history: str, main_issue: str) -> str:
    response = session.post(
        f"{base}/api/v2/preview",
        json={"medical_history": medical_history, "main_issue": main_issue},
        timeout=30,
    )
    body = response.json()
    token = body.get("preview_token")
    if response.status_code != 200 or not token:
        raise RuntimeError(
            f"脱敏预览失败（HTTP {response.status_code}）："
            f"{body.get('advice') or body.get('error') or '未返回预览凭证'}"
        )
    return token
