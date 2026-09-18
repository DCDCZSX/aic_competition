"""HTTP 请求字段校验的轻量公共工具。

Flask 的 ``request.get_json(silent=True) or {}`` 会把多种不同的错误（空请求体、
非法 JSON、JSON 数组）混成一个字典，随后在路由里调用 ``.get`` 可能直接抛出
``AttributeError``，最终返回 HTML 500。本模块只负责纯字段规则，具体的 Flask
响应由 ``app.py`` 统一生成，便于服务层和单元测试复用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


class InputValidationError(ValueError):
    """客户端请求字段不符合接口约定。"""


def string_field(
    data: Dict[str, Any],
    name: str,
    *,
    default: str = "",
    required: bool = False,
    max_length: Optional[int] = None,
) -> str:
    """读取字符串字段，不隐式把对象/数组转换成字符串。"""

    if name not in data or data[name] is None:
        if required:
            raise InputValidationError(f"字段 {name} 不能为空")
        return default
    value = data[name]
    if not isinstance(value, str):
        raise InputValidationError(f"字段 {name} 必须是字符串")
    if max_length is not None and len(value) > max_length:
        raise InputValidationError(f"字段 {name} 过长（最多 {max_length} 个字符）")
    if required and not value.strip():
        raise InputValidationError(f"字段 {name} 不能为空")
    return value


def optional_int(data: Dict[str, Any], name: str) -> Optional[int]:
    """读取可选整数；接受数字字符串，拒绝布尔值和小数。"""

    if name not in data or data[name] is None or data[name] == "":
        return None
    value = data[name]
    if isinstance(value, bool):
        raise InputValidationError(f"字段 {name} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip(), 10)
        except ValueError as exc:
            raise InputValidationError(f"字段 {name} 必须是整数") from exc
    raise InputValidationError(f"字段 {name} 必须是整数")


def string_list(
    data: Dict[str, Any],
    name: str,
    *,
    default: Optional[Sequence[str]] = None,
    max_items: Optional[int] = None,
) -> List[str]:
    """读取字符串数组，不接受逗号拼接字符串或混合类型。"""

    if name not in data or data[name] is None:
        return list(default or ())
    value = data[name]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise InputValidationError(f"字段 {name} 必须是字符串数组")
    if max_items is not None and len(value) > max_items:
        raise InputValidationError(f"字段 {name} 最多包含 {max_items} 项")
    return list(value)


def object_field(data: Dict[str, Any], name: str, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读取 JSON 对象字段。"""

    if name not in data or data[name] is None:
        return dict(default or {})
    value = data[name]
    if not isinstance(value, dict):
        raise InputValidationError(f"字段 {name} 必须是 JSON 对象")
    return value


def phases_field(data: Dict[str, Any], allowed: Sequence[str]) -> Optional[List[str]]:
    """读取分析相位列表并校验白名单。"""

    if "phases" not in data or data["phases"] is None:
        return None
    phases = string_list(data, "phases", max_items=len(allowed))
    unknown = [phase for phase in phases if phase not in allowed]
    if unknown:
        raise InputValidationError(
            f"phases 包含不支持的相位：{'、'.join(unknown)}；可选：{'、'.join(allowed)}"
        )
    # 去重但保持客户端顺序，避免重复启动同一相位。
    return list(dict.fromkeys(phases))
