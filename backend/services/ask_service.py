"""智能追问 v1（规格书 §2 P0 链路第 5 步、§6.1、§10）。

规则（写死）：
- 只问**高价值问题**：缺失字段 ∈ 依赖矩阵，且影响 ≥1 个专科判断（`dependency_matrix.is_high_value`）；
- **≤1 轮、≤3 问**（比赛版限制，`MAX_QUESTIONS`）；
- 每个问题必须显示：为什么问、会影响哪些判断、可否"不知道"、可否跳过；
- 排序优先**影响分诊**的字段 —— 保证"影响分诊/候选集的问题占比 ≥50%"（规格书 §10 验收线）；
- 患者跳过关键问题 → 调用方应**降低可靠性等级**并在摘要标注缺口，**不得用默认值伪造完整信息**（主文档 §5.5）。

用法：
    questions = build_questions(gaps=profile.information_gaps, departments=["内分泌科", "肾内科", "眼科"])
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import dependency_matrix as dm  # noqa: E402

# 比赛版限制：≤1 轮、≤3 问
MAX_QUESTIONS = 3

# 患者可以回答"不知道"/跳过
ALWAYS_SKIPPABLE = True
UNKNOWN_ANSWER = "不清楚 / 跳过"


@dataclass
class Question:
    """一个追问项。字段与文案均对齐 UI 设计说明 P5。"""

    field: str
    text: str                       # 问题正文（口语）
    why: str                        # 为什么问
    affects: List[str] = field(default_factory=list)   # 会影响哪些判断（专科）
    affects_triage: bool = False    # 是否影响分诊方向
    skippable: bool = True          # 是否可跳过
    answerable_unknown: bool = True # 是否可以回答"不知道"
    options: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "field": self.field,
            "text": self.text,
            "why": self.why,
            "affects": list(self.affects),
            "affects_triage": self.affects_triage,
            "skippable": self.skippable,
            "answerable_unknown": self.answerable_unknown,
            "options": list(self.options),
            "unknown_answer": UNKNOWN_ANSWER,
        }


def _why(dep: dm.FieldDependency) -> str:
    targets = "、".join(dep.departments[:3])
    tail = "，也会影响先看哪个科的判断" if dep.affects_triage else ""
    return f"这条信息会影响 {targets} 的判断{tail}"


def build_questions(
    *,
    gaps: Iterable[str],
    departments: Optional[Sequence[str]] = None,
    limit: int = MAX_QUESTIONS,
    answered_fields: Optional[Iterable[str]] = None,
) -> List[Question]:
    """根据画像缺口生成追问列表（已按价值排序，最多 `limit` 条）。"""
    limit = max(0, min(int(limit), MAX_QUESTIONS))
    if limit == 0:
        return []

    answered = set(answered_fields or [])
    candidates: List[dm.FieldDependency] = []

    for gap in gaps or []:
        text = str(gap)
        for label, field_key in dm.BY_LABEL.items():
            if label in text:
                if field_key in answered:
                    break
                if not dm.is_high_value(field_key, departments):
                    break
                dep = dm.depends_on(field_key)
                if dep and dep not in candidates:
                    candidates.append(dep)
                break

    # 排序：影响分诊优先 → 影响专科数多优先 → 保持矩阵顺序稳定
    candidates.sort(key=lambda d: (not d.affects_triage, -len(d.departments)))

    questions: List[Question] = []
    for dep in candidates[:limit]:
        questions.append(
            Question(
                field=dep.field,
                text=dep.question or f"能否补充一下「{dep.label}」的情况？",
                why=_why(dep),
                affects=list(dep.departments),
                affects_triage=dep.affects_triage,
                skippable=ALWAYS_SKIPPABLE,
                options=list(dep.options),
            )
        )
    return questions


def triage_related_ratio(questions: Sequence[Question]) -> float:
    """影响分诊/候选集的问题占比（规格书 §10 验收：≥50%）。"""
    if not questions:
        return 0.0
    return sum(1 for q in questions if q.affects_triage) / len(questions)


def apply_answers(
    gaps: Iterable[str],
    answers: Dict[str, str],
) -> Dict[str, object]:
    """把患者回答并入画像缺口处理结果。

    - 已回答：从缺口移除；
    - 回答"不知道/跳过"：保留缺口，并标记 `unresolved`（用于降低可靠性等级，**不编造**）。
    """
    remaining: List[str] = []
    resolved: List[str] = []
    unresolved: List[str] = []

    for gap in gaps or []:
        text = str(gap)
        matched_field: Optional[str] = None
        for label, field_key in dm.BY_LABEL.items():
            if label in text:
                matched_field = field_key
                break

        if matched_field and matched_field in answers:
            answer = str(answers[matched_field]).strip()
            if answer and answer not in (UNKNOWN_ANSWER, "不知道", "跳过", "不清楚"):
                resolved.append(matched_field)
                continue
            unresolved.append(matched_field)
            remaining.append(text)
            continue

        remaining.append(text)

    return {"remaining_gaps": remaining, "resolved_fields": resolved,
            "unresolved_fields": unresolved}


def needs_reliability_downgrade(unresolved_fields: Sequence[str]) -> bool:
    """未解决的关键缺口是否应触发可靠性降级（主文档 §5.5）。"""
    return len(unresolved_fields) > 0
