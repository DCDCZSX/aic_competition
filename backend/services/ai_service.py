"""LLM 调用服务（OpenAI 兼容网关）。

安全约定（务必遵守）：
- **绝不把 API Key 写进代码或提交到 git**；
- 密钥只从环境变量读取：`AI_API_KEY`（必填），可选 `AI_API_URL` / `AI_MODEL_NAME` /
  `AI_CONNECT_TIMEOUT` / `AI_READ_TIMEOUT` / `AI_MAX_RETRIES`；`AI_TIMEOUT` 仅作旧配置兼容；
- 本地开发在 `CHAIS/.env` 或 `backend/.env` 配置（`.env` 已被 .gitignore 忽略），
  模板见仓库根目录 `.env.example`。
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# 默认值：可被环境变量覆盖
DEFAULT_API_URL = "https://openai.uniin.cn/openapi/v2/chat/completions"
DEFAULT_MODEL_NAME = "qwen-max-latest"

# 单次调用的输出预算（token）。结构化输出（多学科意见卡等）由调用方按需调大：
# 实测 deepseek-chat 的血液科意见卡需要 ~2335 token，用 2000 会被截断成非法 JSON。
DEFAULT_MAX_TOKENS = 2000


def call_structured(ai_service: Any, prompt: str, role_description: str = None,
                    max_tokens: int = None) -> Tuple[str, bool, str]:
    """调用模型并返回 `(正文, 是否被 max_tokens 截断, 错误)`。

    **结构化输出（JSON）的调用方统一用它**，原因：
    - 优先走 `AIService.call_ai_api_raw`：不做 Markdown 清理（清理会把 ```json
      包裹的整段内容删掉、也会改动字符串内容），并且能拿到 `finish_reason`
      ——截断是"意见卡解析失败"的真实原因之一；
    - 测试替身只实现 `call_ai_api` 时自动降级，行为不变；
    - 网关异常按"错误"返回，由调用方决定降级方式，不让一个科室的失败连带整条链路。
    """
    raw_fn = getattr(ai_service, "call_ai_api_raw", None)
    if callable(raw_fn):
        try:
            result = raw_fn(prompt, role_description=role_description, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            return "", False, f"{type(exc).__name__}: {exc}"
        content = str(result.get("content") or "")
        if result.get("ok"):
            return content, bool(result.get("truncated")), ""
        return content, bool(result.get("truncated")), str(result.get("error") or "调用失败")
    try:
        content = ai_service.call_ai_api(prompt, role_description=role_description)
    except Exception as exc:  # noqa: BLE001
        return "", False, f"{type(exc).__name__}: {exc}"
    return str(content or ""), False, "" if content else "模型未返回内容"


class AIService:
    def __init__(self):
        self.api_url = os.getenv("AI_API_URL", DEFAULT_API_URL).strip()
        self.api_key = (os.getenv("AI_API_KEY") or "").strip()
        self.model_name = os.getenv("AI_MODEL_NAME", DEFAULT_MODEL_NAME).strip()
        # requests 的 timeout 单位是秒。旧默认值 3000 会让单次请求卡住 50 分钟。
        self.connect_timeout = max(1, int(os.getenv("AI_CONNECT_TIMEOUT", "10")))
        self.read_timeout = max(1, int(os.getenv("AI_READ_TIMEOUT",
                                                os.getenv("AI_TIMEOUT", "60"))))
        self.max_retries = int(os.getenv("AI_MAX_RETRIES", "3"))

        if not self.api_key:
            logger.warning(
                "[AIService] 警告：未检测到环境变量 AI_API_KEY，LLM 调用将被跳过。\n"
                "           请在 CHAIS/.env 或 backend/.env 中配置（参考 .env.example）。\n"
                "           注意：不要把密钥写回代码或提交到 git。"
            )

    def call_ai_api(self, prompt: str, role_description: str = None,
                    max_tokens: int = None) -> Optional[str]:
        """调用AI API（返回正文，保留原有的 Markdown 清理，供纯文本用途）。"""
        result = self.call_ai_api_raw(prompt, role_description=role_description,
                                      max_tokens=max_tokens)
        if not result.get("ok"):
            return None
        return self.markdown_to_text(result["content"])

    def call_ai_api_raw(self, prompt: str, role_description: str = None,
                        max_tokens: int = None) -> Dict[str, Any]:
        """调用模型并返回结构化结果（**结构化输出的调用方必须用这个**）。

        返回：`{"ok", "content", "finish_reason", "truncated", "error"}`

        为什么必须区分：① 原来的接口会对正文做 Markdown 清理，而清理会把
        ```json 包裹的整段内容**删掉**、也会改动字符串里的内容，对 JSON 输出有害；
        ② 原来的接口拿不到 `finish_reason`，于是 `max_tokens` 截断（真实故障：
        血液科意见卡 3780 字符被截断成非法 JSON）只能表现成"解析失败"，看不出真因。
        """
        if not self.api_key:
            logger.error("[AIService] 错误：缺少 AI_API_KEY，已跳过本次调用。")
            return {"ok": False, "content": "", "finish_reason": "", "truncated": False,
                    "error": "未配置 AI_API_KEY"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        messages = []
        if role_description:
            messages.append({"role": "system", "content": role_description})
        messages.append({"role": "user", "content": prompt})

        data = {
            "model": self.model_name,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": int(max_tokens or DEFAULT_MAX_TOKENS),
        }

        last_error = ""
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.api_url,
                    headers=headers,
                    json=data,
                    timeout=(self.connect_timeout, self.read_timeout)
                )
                response.raise_for_status()
                result = response.json()

                if 'choices' in result and len(result['choices']) > 0:
                    choice = result['choices'][0]
                    content = (choice.get("message") or {}).get("content") or ""
                    finish = str(choice.get("finish_reason") or "")
                    truncated = finish == "length"
                    if truncated:
                        logger.warning(
                            "[AIService] 警告：输出被 max_tokens=%s 截断 "
                            "（finish_reason=length，%d 字符），调用方需按截断处理",
                            data["max_tokens"], len(content))
                    return {"ok": True, "content": content, "finish_reason": finish,
                            "truncated": truncated, "error": ""}
                if isinstance(result, dict):
                    top_level_keys = sorted(str(key) for key in result)
                    choices = result.get("choices")
                    choices_count = len(choices) if isinstance(choices, list) else 0
                else:
                    top_level_keys = []
                    choices_count = 0
                logger.warning(
                    "API response shape invalid: top_level_keys=%s "
                    "top_level_key_count=%d choices_count=%d content_length=0",
                    top_level_keys, len(top_level_keys), choices_count)
                return {"ok": False, "content": "", "finish_reason": "", "truncated": False,
                        "error": "API 响应格式异常"}

            except requests.exceptions.RequestException as e:
                last_error = str(e)
                status_code = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(
                    "API request failed: exception_type=%s attempt=%d/%d status_code=%s",
                    type(e).__name__, attempt + 1, self.max_retries, status_code)
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)  # 指数退避
                    continue
            except Exception as e:
                logger.error("API processing failed: exception_type=%s", type(e).__name__)
                return {"ok": False, "content": "", "finish_reason": "", "truncated": False,
                        "error": str(e)}
        return {"ok": False, "content": "", "finish_reason": "", "truncated": False,
                "error": last_error or "调用失败"}

    def markdown_to_text(self, markdown_text: str) -> str:
        """将Markdown格式转换为整洁的文本格式"""
        if not markdown_text:
            return ""
        
        text = markdown_text
        
        # 移除代码块
        text = re.sub(r'```[\s\S]*?```', '', text)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        
        # 处理标题
        text = re.sub(r'^#{1,6}\s*(.+)$', r'\1', text, flags=re.MULTILINE)
        
        # 处理粗体和斜体
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        text = re.sub(r'__([^_]+)__', r'\1', text)
        text = re.sub(r'_([^_]+)_', r'\1', text)
        
        # 处理链接
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        
        # 处理列表项
        text = re.sub(r'^[\s]*[-*+]\s+', '• ', text, flags=re.MULTILINE)
        text = re.sub(r'^\d+\.\s+', '• ', text, flags=re.MULTILINE)
        
        # 清理多余的空行
        text = re.sub(r'\n\s*\n\s*\n', '\n\n', text)
        text = text.strip()
        
        return text

    def extract_json_from_text(self, text: str) -> Optional[dict]:
        """从文本中提取JSON"""
        try:
            # 尝试直接解析
            return json.loads(text)
        except:
            try:
                # 查找JSON块
                start = text.find('{')
                end = text.rfind('}') + 1
                if start != -1 and end > start:
                    json_str = text[start:end]
                    return json.loads(json_str)
            except:
                pass
        return None
