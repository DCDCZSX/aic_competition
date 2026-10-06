"""Optional vision OCR adapter for patient-uploaded images."""
from __future__ import annotations

import base64
import os
from typing import Any, Dict

import requests


class OcrService:
    def __init__(self) -> None:
        self.api_key = (os.getenv("AI_API_KEY") or "").strip()
        self.api_url = os.getenv("AI_API_URL", "https://openai.uniin.cn/openapi/v2/chat/completions").strip()
        self.model_name = os.getenv("AI_VISION_MODEL_NAME", os.getenv("AI_MODEL_NAME", "qwen-vl-max")).strip()
        self.timeout = max(5, min(int(os.getenv("AI_OCR_TIMEOUT", "45")), 120))

    def extract(self, data: bytes, content_type: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "code": "ocr_unavailable", "error": "暂未配置图片识别服务，请手动填写病情"}
        encoded = base64.b64encode(data).decode("ascii")
        payload = {
            "model": self.model_name,
            "temperature": 0,
            "max_tokens": 3000,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "请准确转录这张医疗资料图片中的文字。只返回识别到的文字，不要诊断、总结或补充不存在的内容。"},
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{encoded}"}},
            ]}],
        }
        try:
            response = requests.post(self.api_url, headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, json=payload, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if isinstance(content, list):
                content = "\n".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
            text = str(content).strip()[:12000]
            if not text:
                return {"ok": False, "code": "ocr_empty", "error": "图片中没有识别到可用文字"}
            return {"ok": True, "text": text}
        except Exception:
            return {"ok": False, "code": "ocr_failed", "error": "图片识别暂时失败，请检查图片或手动填写"}
