"""模型生成文本的服务端安全兜底。

Prompt 约束不是安全边界。本模块只检查模型自行生成的判断/建议字段，不检查患者
提供的客观病史事实，避免把“既往诊断为……”这类合法资料误判为模型越权诊断。
"""

from __future__ import annotations

import re
from typing import Any, Iterable


class UnsafeModelOutput(ValueError):
    """模型输出包含不能直接进入病例或报告的医疗指令。"""


_PROBABILITY_NUMBER = re.compile(
    r"(?:"
    r"(?:概率|置信度|可能性|confidence|probability|score)\s*"
    r"(?:为|是|=|[:：])?\s*\d+(?:\.\d+)?\s*%?"
    r"|\d+(?:\.\d+)?\s*%\s*(?:的)?\s*(?:概率|置信度|可能性|confidence)"
    r")",
    re.IGNORECASE,
)
_DEFINITE_DIAGNOSIS = re.compile(r"(?:已经|已|明确|最终|因此)?(?:确诊为|诊断为)")
_ACTION_LEAD = r"(?:立即|立刻|马上|自行|直接|必须|应当|应该|务必|建议|推荐|请|可考虑|宜)"
_TREATMENT_ACTION = (
    r"(?:开始|进行|接受|采用|改为|继续|规律)?\s*"
    r"(?:服用|口服|服药|用药|使用.{0,10}?药|停用|停药|加量|减量|换药|注射|静滴|输液|"
    r"化疗|放疗|手术|治疗|理疗|介入)"
)
_TREATMENT_COMMAND = re.compile(
    rf"(?:^|[。；;！!：:\n])\s*(?:立即|立刻|马上|自行|直接)\s*{_TREATMENT_ACTION}"
    rf"|{_ACTION_LEAD}\s*{_TREATMENT_ACTION}"
    rf"|{_ACTION_LEAD}(?!\s*(?:向|请)医生确认)(?:行|实施|选择|接受|采用|安排)?\s*.{{0,12}}?"
    r"(?:手术|药物治疗|保守治疗|对症治疗|抗感染治疗|激素治疗)",
    re.IGNORECASE,
)
_DOSE_INSTRUCTION = re.compile(
    r"(?:立即|立刻|马上|自行|直接|必须|应当|务必|建议|请)\s*"
    r"(?:服用|口服|注射|静滴|加量|减量).{0,24}"
    r"\d+(?:\.\d+)?\s*(?:mg|g|ml|毫克|克|毫升|片|粒|次)",
    re.IGNORECASE,
)
_NEGATIONS = ("不", "未", "不能", "无法", "尚不能", "不可", "并非", "需排查", "待排除")


def public_text(value: Any) -> str:
    """删除各种前后缀形式的模型自报概率；不改动普通检验数值。"""
    return _PROBABILITY_NUMBER.sub("模型数值已隐藏", str(value or "")).strip()


def _has_unnegated_diagnosis(text: str) -> bool:
    for match in _DEFINITE_DIAGNOSIS.finditer(text):
        prefix = text[max(0, match.start() - 5):match.start()]
        if not any(token in prefix for token in _NEGATIONS):
            return True
    return False


def assert_safe_generated_text(value: Any, *, field_name: str,
                               allow_definitive_diagnosis: bool = False) -> str:
    """校验模型生成的判断/建议字段；违规时整项拒绝，不回显违规原文。"""
    text = public_text(value)
    if not allow_definitive_diagnosis and _has_unnegated_diagnosis(text):
        raise UnsafeModelOutput(f"{field_name}包含确定性诊断表述")
    if _TREATMENT_COMMAND.search(text) or _DOSE_INSTRUCTION.search(text):
        raise UnsafeModelOutput(f"{field_name}包含用药或治疗指令")
    return text


def assert_safe_generated_list(values: Iterable[Any], *, field_name: str) -> list[str]:
    return [assert_safe_generated_text(value, field_name=field_name) for value in values]


def assert_safe_generated_fact(value: Any, *, field_name: str = "客观事实") -> str:
    """校验模型摘取的事实：允许既往诊疗事实，但不允许夹带新的治疗建议。"""
    return assert_safe_generated_text(
        value, field_name=field_name, allow_definitive_diagnosis=True)
