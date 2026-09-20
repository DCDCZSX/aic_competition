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
    def __init__(self, ttl_minutes: int = 10, per_owner_limit: int = 5,
                 global_limit: int = 5000) -> None:
        self.ttl = timedelta(minutes=max(1, min(ttl_minutes, 30)))
        self.per_owner_limit = max(1, int(per_owner_limit))
        self.global_limit = max(1, int(global_limit))
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
            owner_count = sum(item.owner_id == owner_id for item in self._items.values())
            while owner_count >= self.per_owner_limit:
                self._evict_oldest_locked(owner_id)
                owner_count -= 1
            while len(self._items) >= self.global_limit:
                self._evict_oldest_locked()
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

    def _evict_oldest_locked(self, owner_id: Optional[str] = None) -> None:
        token_to_evict = None
        for token, payload in self._items.items():
            if owner_id is None or payload.owner_id == owner_id:
                token_to_evict = token
                break
        if token_to_evict is not None:
            self._items.pop(token_to_evict)

    def _purge_expired_locked(self) -> None:
        now = datetime.now()
        expired = [token for token, item in self._items.items() if item.expires_at <= now]
        for token in expired:
            self._items.pop(token, None)
