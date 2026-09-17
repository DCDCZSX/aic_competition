"""Short-lived, one-time intake previews bound to an authenticated user."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional


@dataclass(frozen=True)
class PreviewPayload:
    owner_id: str
    medical_history: str
    main_issue: str
    expires_at: datetime


class PreviewTokenService:
    def __init__(self, ttl_minutes: int = 10) -> None:
        self.ttl = timedelta(minutes=max(1, min(ttl_minutes, 30)))
        self._items: Dict[str, PreviewPayload] = {}
        self._lock = threading.Lock()

    def issue(self, owner_id: str, medical_history: str, main_issue: str) -> str:
        token = secrets.token_urlsafe(24)
        payload = PreviewPayload(
            owner_id=owner_id,
            medical_history=medical_history,
            main_issue=main_issue,
            expires_at=datetime.now() + self.ttl,
        )
        with self._lock:
            self._purge_expired_locked()
            self._items[token] = payload
        return token

    def consume(self, token: str, owner_id: str) -> Optional[PreviewPayload]:
        if not token or not owner_id:
            return None
        with self._lock:
            self._purge_expired_locked()
            payload = self._items.get(token)
            if payload is None or payload.owner_id != owner_id:
                return None
            self._items.pop(token, None)
            return payload

    def _purge_expired_locked(self) -> None:
        now = datetime.now()
        expired = [token for token, item in self._items.items() if item.expires_at <= now]
        for token in expired:
            self._items.pop(token, None)
