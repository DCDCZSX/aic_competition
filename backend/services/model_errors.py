"""稳定、脱敏的模型调用错误分类。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import requests


@dataclass(frozen=True)
class ModelError:
    """可记录到业务结果中的模型错误；不携带上游异常详情。"""

    code: str
    message: str


_MESSAGES = {
    "ai_missing_api_key": "模型服务未配置",
    "ai_timeout": "模型服务响应超时",
    "ai_auth_failed": "模型服务认证失败",
    "ai_rate_limited": "模型服务暂时限流",
    "ai_upstream_unavailable": "模型服务暂时不可用",
    "ai_invalid_response": "模型服务返回格式异常",
    "ai_request_failed": "模型服务调用失败",
}


def error_for_code(code: str) -> ModelError:
    """根据受控错误码生成稳定消息；未知码统一降级。"""
    safe_code = code if code in _MESSAGES else "ai_request_failed"
    return ModelError(safe_code, _MESSAGES[safe_code])


def invalid_model_response() -> ModelError:
    return error_for_code("ai_invalid_response")


def classify_model_exception(
    exc: BaseException,
    *,
    status_code: Optional[int] = None,
) -> ModelError:
    """把异常分类为稳定错误，不把异常文本带入返回值。"""
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)

    if isinstance(exc, requests.Timeout):
        return error_for_code("ai_timeout")
    if status_code in (401, 403):
        return error_for_code("ai_auth_failed")
    if status_code == 429:
        return error_for_code("ai_rate_limited")
    if isinstance(status_code, int) and status_code >= 500:
        return error_for_code("ai_upstream_unavailable")
    return error_for_code("ai_request_failed")
