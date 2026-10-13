"""Shared effective evidence and confirmed demographics for every analysis path."""
from copy import deepcopy
from typing import Any, Dict, Optional
from services.dependency_matrix import BY_FIELD

UNKNOWN_ANSWERS = {"不清楚 / 跳过", "不清楚", "跳过", "不知道"}


def effective_case_text(payload: Dict[str, Any], *, answers: Optional[Dict[str, str]] = None) -> str:
    history = str(payload.get("masked_history") or "")
    issue = str(payload.get("masked_main_issue") or "")
    parts = [history]
    if issue and issue not in history:
        parts.append(issue)
    values = (payload.get("answers") or {}) if answers is None else answers
    parts.extend(f"{BY_FIELD[key].label if key in BY_FIELD else key}：{value.strip()}" for key, value in values.items()
                 if isinstance(value, str) and value.strip() and value.strip() not in UNKNOWN_ANSWERS)
    return "；".join(part for part in parts if part)


def confirmed_demographics(payload: Dict[str, Any]) -> Dict[str, Any]:
    if "demographics" in payload:
        return dict(payload.get("demographics") or {})
    # Older progressive cases stored confirmed form values in the rule profile.
    rules = payload.get("profile_rules") or {}
    return {key: rules[key] for key in ("age", "sex") if rules.get(key) not in (None, "")}


def merge_demographics(profile: Dict[str, Any], confirmed: Dict[str, Any]) -> Dict[str, Any]:
    result = deepcopy(profile)
    conflicts = []
    for key in ("age", "sex"):
        value = confirmed.get(key)
        if value in (None, ""):
            continue
        extracted = result.get(key)
        if extracted not in (None, "", "未知") and extracted != value:
            label = "年龄" if key == "age" else "性别"
            conflicts.append({"field": key, "confirmed": value, "extracted": extracted,
                              "message": f"{label}与正文记录不一致，已保留表单值，请核对原始资料"})
        result[key] = value
    result["demographic_conflicts"] = conflicts
    return result
