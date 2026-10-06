"""Authenticated temporary storage for patient-provided image materials."""
from __future__ import annotations
import json, os, re, secrets, tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_UPLOAD_COUNT = 5
class PatientUploadStore:
    def __init__(self, directory: str | os.PathLike[str], max_bytes: int = MAX_UPLOAD_BYTES) -> None:
        self.directory = Path(directory).resolve(); self.directory.mkdir(parents=True, exist_ok=True); self.max_bytes = max(1, int(max_bytes))
    def save(self, owner_id: str, original_name: str, content_type: str, data: bytes) -> Dict[str, Any]:
        content_type = str(content_type or "").lower().split(";", 1)[0].strip()
        if content_type not in ALLOWED_TYPES: return {"ok": False, "code": "unsupported_type", "error": "只支持 JPG、PNG 或 WEBP 图片"}
        if len(data) > self.max_bytes: return {"ok": False, "code": "file_too_large", "error": f"单张图片不能超过 {self.max_bytes // (1024 * 1024)}MB"}
        if not data: return {"ok": False, "code": "empty_file", "error": "图片文件为空"}
        upload_id = secrets.token_urlsafe(24); stored_name = upload_id + ALLOWED_TYPES[content_type]
        metadata = {"upload_id": upload_id, "owner_id": str(owner_id), "original_name": Path(str(original_name or "image")).name[:160], "stored_name": stored_name, "content_type": content_type, "size": len(data), "created_at": datetime.now(timezone.utc).isoformat()}
        target = self.directory / stored_name; meta_target = self.directory / (upload_id + ".json")
        with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".upload-", delete=False) as handle: temp_name = Path(handle.name); handle.write(data)
        try: os.replace(temp_name, target); meta_target.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        except Exception: temp_name.unlink(missing_ok=True); target.unlink(missing_ok=True); meta_target.unlink(missing_ok=True); raise
        return metadata | {"ok": True}
    def delete(self, owner_id: str, upload_id: str) -> bool:
        metadata = self._metadata(upload_id)
        if not metadata or metadata.get("owner_id") != owner_id: return False
        (self.directory / metadata["stored_name"]).unlink(missing_ok=True); (self.directory / (upload_id + ".json")).unlink(missing_ok=True); return True
    def read_owned(self, owner_id: str, upload_id: str):
        metadata = self._metadata(upload_id)
        if not metadata or metadata.get("owner_id") != owner_id:
            return None, None
        try:
            return metadata, (self.directory / metadata["stored_name"]).read_bytes()
        except OSError:
            return None, None
    def list_owned(self, owner_id: str):
        items = []
        for meta_path in self.directory.glob("*.json"):
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(metadata, dict) or metadata.get("owner_id") != owner_id:
                continue
            if not UPLOAD_ID_RE.fullmatch(str(metadata.get("upload_id") or "")):
                continue
            if not (self.directory / str(metadata.get("stored_name") or "")).is_file():
                continue
            items.append({key: metadata.get(key) for key in (
                "upload_id", "original_name", "content_type", "size", "created_at"
            )})
        return sorted(items, key=lambda item: str(item.get("created_at") or ""))
    def _metadata(self, upload_id: str) -> Dict[str, Any] | None:
        if not UPLOAD_ID_RE.fullmatch(str(upload_id or "")): return None
        try: data = json.loads((self.directory / (upload_id + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError): return None
        return data if isinstance(data, dict) else None
