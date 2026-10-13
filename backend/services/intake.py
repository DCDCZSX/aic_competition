"""患者端录入校验与就医目标（规格书 §2 P0 链路第 1-2 步、§4.4 G2/G3）。

职责：
1. **就医目标**（UI 设计说明 P1）：一个主选项 + 可折叠特殊场景，全部可跳过；
   目标只调整"追问侧重点 / 任务排序 / 展示重点"，**不决定分诊结论**（主文档 §5.1 红线）。
2. **录入校验**：主诉与主要问题双空 → G2 阻断；命中 PII → G3 阻断；长度/数值域校验。

用法：
    result = validate_intake(medical_history=text, main_issue="", goals=["multi_disease"])
    if not result.ok:
        return render_errors(result.errors)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.privacy import DesensitizeResult, desensitize  # noqa: E402
from services.guardrail import screen  # noqa: E402

# 病历正文长度上限（规格书 §4.4 G2 相关：超长引导分批）
MAX_TEXT_LENGTH = 8000
# 主要问题通常是短描述；单独限制，避免绕过病历正文上限后把大段文本送入分析链路。
MAX_MAIN_ISSUE_LENGTH = 2000

# 年龄合理区间
MIN_AGE, MAX_AGE = 1, 129
VALID_SEX = ("", "男", "女", "其他", "未知")


def normalize_sex(value: str) -> str:
    if not isinstance(value, str) or value.strip() not in VALID_SEX:
        raise ValueError("性别必须为男、女、其他、未知或留空")
    return value.strip()


# ---------------------------------------------------------------------------
# 一、就医目标（人话版，患者界面只出现这些文案）
# ---------------------------------------------------------------------------

PRIMARY_GOAL: Dict[str, str] = {
    "id": "unknown_department",
    "title": "我/家人身体不舒服，不知道先该看哪个科、怎么办",
    "desc": "推荐选择：多数人都是这种情况",
}

ADVANCED_GOALS: List[Dict[str, str]] = [
    {"id": "multi_disease", "title": "有好几种老毛病，想一起理清楚",
     "desc": "跨系统、多病共存的情况"},
    {"id": "compare_opinions", "title": "几个医生说法不太一样，想对比一下",
     "desc": "已有多位医生的意见"},
    {"id": "surgery_prep", "title": "要做手术/治疗，想提前把想问的问题准备好",
     "desc": "术前/介入前的准备"},
]

ALL_GOAL_IDS: List[str] = [PRIMARY_GOAL["id"]] + [g["id"] for g in ADVANCED_GOALS]

# 目标 → 行为类型"提示"（仅影响追问重点与任务排序，不直接决定分诊；见规格书 §1 三类修正点）
GOAL_BEHAVIOR_HINT: Dict[str, Optional[str]] = {
    "unknown_department": None,   # 由病例资料自动判定 A/B/C/D
    "multi_disease": "B",
    "compare_opinions": None,     # 作为 B/C/D 上的"意见对比"增强模式
    "surgery_prep": "D",
}


# ---------------------------------------------------------------------------
# 二、录入校验（G2 / G3 / 长度 / 数值域）
# ---------------------------------------------------------------------------

@dataclass
class IntakeResult:
    """一次录入的校验结果。"""

    ok: bool = True
    errors: List[str] = field(default_factory=list)      # 阻断原因（对应 G1/G2/G3 等）
    warnings: List[str] = field(default_factory=list)    # 不阻断，但需提示
    goals: List[str] = field(default_factory=list)       # 已选目标
    behavior_hint: Optional[str] = None                  # 目标给出的行为类型提示
    text_length: int = 0
    pii: Optional[DesensitizeResult] = None
    screen: Optional[Any] = None                         # G1/G6 输入侧拦截结果

    @property
    def blocked_by(self) -> str:
        """阻断来源，便于日志与前端定位（G1 / G2 / G3 / input）。"""
        for code in ("G1", "G2", "G3", "G6"):
            if any(code in e for e in self.errors):
                return code
        return "input" if self.errors else ""

    @property
    def is_crisis(self) -> bool:
        """是否因急症被拦截（前端需展示急救提示页）。"""
        return bool(self.screen is not None and getattr(self.screen, "is_crisis", False))


def _clean(value: Any) -> str:
    return ("" if value is None else str(value)).strip()


def validate_intake(
    *,
    medical_history: str = "",
    main_issue: str = "",
    goals: Optional[List[str]] = None,
    age: Optional[Any] = None,
    sex: str = "",
    name: str = "",
) -> IntakeResult:
    """校验一次录入。返回 `IntakeResult`，`ok=False` 表示应阻断进入下一步。"""
    history = _clean(medical_history)
    issue = _clean(main_issue)
    goals = list(goals or [])
    combined = "\n".join(x for x in (history, issue) if x)
    result = IntakeResult(goals=goals, text_length=len(history))

    # ---- G1 危象拦截（安全优先，最先判定）----
    screen_result = screen(combined)
    result.screen = screen_result
    if screen_result.blocked:
        result.errors.append(f"{screen_result.code}：{screen_result.advice}")
        for reason in screen_result.reasons:
            result.warnings.append(f"触发依据：{reason}")
        result.ok = False
        return result  # 危象/非医学内容：立即阻断，不做后续校验

    # ---- 目标校验（不阻断，仅提示）----
    invalid_goals = [g for g in goals if g not in ALL_GOAL_IDS]
    if invalid_goals:
        result.warnings.append(f"未识别的就医目标已忽略：{', '.join(invalid_goals)}")
        result.goals = [g for g in goals if g in ALL_GOAL_IDS]
    if not result.goals:
        result.warnings.append("未选择就医目标：将根据你提供的资料自动判断（可在画像确认时校正）")

    # 行为类型提示：取第一个有提示的目标（优先级由 UI 顺序决定）
    for goal in result.goals:
        hint = GOAL_BEHAVIOR_HINT.get(goal)
        if hint:
            result.behavior_hint = hint
            break

    # ---- G2：主诉与主要问题双空 ----
    if not history and not issue:
        result.errors.append("G2：请至少填写病情描述或主要问题（两者不能同时为空）")

    # ---- 长度上限 ----
    if len(history) > MAX_TEXT_LENGTH:
        result.errors.append(
            f"病历正文过长（{len(history)} 字 > {MAX_TEXT_LENGTH} 字）：请精简，或分批录入资料"
        )
    if len(issue) > MAX_MAIN_ISSUE_LENGTH:
        result.errors.append(
            f"主要问题过长（{len(issue)} 字 > {MAX_MAIN_ISSUE_LENGTH} 字）：请精简后再提交"
        )

    # ---- 数值域 ----
    if age not in (None, "", "None"):
        try:
            age_value = int(age)
            if not (MIN_AGE <= age_value <= MAX_AGE):
                result.errors.append(f"年龄 {age_value} 超出合理范围（{MIN_AGE}-{MAX_AGE}）")
        except (TypeError, ValueError):
            result.errors.append(f"年龄格式不正确：{age!r}")

    try:
        normalize_sex(sex)
    except ValueError as exc:
        result.errors.append(str(exc))

    # ---- G3：PII 检测（阻断分析，提示清除）----
    pii = desensitize(combined)
    result.pii = pii
    if pii.blocked:
        kinds = "、".join(sorted({h.label for h in pii.blocking_hits}))
        result.errors.append(
            f"G3：检测到敏感信息（{kinds}），请先清除后再提交；"
            f"这些内容会直接暴露个人身份，本系统不做分析"
        )
    elif pii.has_pii:
        kinds = "、".join(sorted({h.label for h in pii.hits}))
        result.warnings.append(f"检测到可能需要打码的内容（{kinds}），提交前会展示脱敏预览")

    if name and _clean(name):
        result.warnings.append("请勿填写真实姓名；如需标识，可使用「张*」这类脱敏写法")

    result.ok = not result.errors
    return result


def sanitized_payload(medical_history: str, main_issue: str = "") -> Dict[str, str]:
    """返回打码后的文本（提交给分析链路用），避免把原文送进 LLM。"""
    history = desensitize(medical_history or "").masked_text
    issue = desensitize(main_issue or "").masked_text
    return {"medical_history": history, "main_issue": issue}


def goal_options() -> Dict[str, Any]:
    """给前端渲染的目标选项（主选项 + 可折叠特殊场景）。"""
    return {"primary": dict(PRIMARY_GOAL), "advanced": [dict(g) for g in ADVANCED_GOALS],
            "skippable": True}
