"""轻量质询（P0 版）——让 AI 自己"被质疑一次"。

对应《产品开发规格书-比赛版v-competition.md》§6.3 与主文档 §8.4。

**P0 轻量实现**（不新增常驻 Agent）：
1. 触发条件：聚合层出现**分歧**或**高影响不确定性**时才发起（无问题则完全跳过）；
2. 由牵头科视角生成 ≤3 条挑战问题（规则驱动，不依赖 LLM 也能产出）；
3. 问题发给**与该问题相关的专科**补答一轮，回应聚焦"补充证据 / 修正观点 / 维持原判断"；
4. 输出**质询前后对照**：哪些分歧被质疑、哪些判断需补证据、哪些需医生裁决。

红线（写死，与主文档 §8.4 一致）：
- **绝不出概率数字或"置信度变化"**，只输出结构化结论；
- 质询结果不替代医生裁决；未收敛的分歧一律标记"需医生裁决"。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CHALLENGE_OUTCOMES, CHALLENGE_STANCES, ChallengeRound  # noqa: E402
from services.ai_service import call_structured  # noqa: E402
from services.json_recovery import loads_lenient  # noqa: E402
from services.prompt_text import case_text_for_prompt  # noqa: E402
from services.model_output_guard import (  # noqa: E402
    assert_safe_generated_list,
    assert_safe_generated_text,
)

MAX_CHALLENGES = 3            # P0：每例最多 3 条质询问题
MAX_RESPONDERS = 2            # 每条问题最多问 2 个科室
METHOD_LLM = "llm"
METHOD_SKIPPED = "skipped"
CHALLENGE_MAX_TOKENS = 2000   # 单条回应很短；被截断时如实标注，不静默

CHALLENGE_SYSTEM_PROMPT = (
    "你是临床多学科讨论中的质询视角。你**不提出最终结论**，只负责检验当前判断的证据是否充分、"
    "是否存在被忽略的反向证据。不做诊断、不开处方、不输出任何置信度或概率数字。输出严格 JSON。"
)


@dataclass
class ChallengeQuestion:
    """一条挑战性问题（规则生成，可解释）。"""

    question: str
    targets: List[str] = field(default_factory=list)   # 应回应此问题的科室
    reason: str = ""                                    # 为什么要问
    based_on: str = ""                                  # 依据（分歧/不确定性/缺口）
    kind: str = "disagreement"                          # disagreement / uncertainty / gap

    def to_dict(self) -> Dict[str, Any]:
        return {"question": self.question, "targets": list(self.targets),
                "reason": self.reason, "based_on": self.based_on, "kind": self.kind}


@dataclass
class ChallengeResult:
    """一轮轻量质询的完整产出。"""

    questions: List[ChallengeQuestion] = field(default_factory=list)
    rounds: List[ChallengeRound] = field(default_factory=list)
    method: str = METHOD_SKIPPED
    skipped_reason: str = ""
    notes: List[str] = field(default_factory=list)

    @property
    def triggered(self) -> bool:
        return bool(self.questions)

    @property
    def needs_doctor_decision(self) -> bool:
        return any(r.outcome in ("需医生裁决", "需补充证据") for r in self.rounds)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered": self.triggered,
            "method": self.method,
            "skipped_reason": self.skipped_reason,
            "questions": [q.to_dict() for q in self.questions],
            "rounds": [
                {"question": r.question, "raised_by": r.raised_by,
                 "responses": list(r.responses), "outcome": r.outcome,
                 "follow_up_evidence": list(r.follow_up_evidence)}
                for r in self.rounds
            ],
            "needs_doctor_decision": self.needs_doctor_decision,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 1) 规则驱动的挑战问题生成（无需 LLM）
# ---------------------------------------------------------------------------

def build_questions(aggregation: Dict[str, Any],
                    cards: Optional[Sequence[Any]] = None) -> List[ChallengeQuestion]:
    """从聚合结果里挑出最值得质询的点（分歧优先，其次高影响缺口）。"""
    questions: List[ChallengeQuestion] = []

    # ① 分歧：直接对着分歧双方发问
    for item in (aggregation.get("disagreements") or []):
        positions = item.get("positions") or {}
        targets = [d for d in positions][:MAX_RESPONDERS]
        if not targets:
            continue
        topic = item.get("topic") or "当前判断"
        questions.append(ChallengeQuestion(
            question=f"关于「{topic}」，{ '、'.join(targets) } 的倾向相反："
                     f"现有临床依据是否足以支持各自的判断？请说明是否有被忽略的反向证据。",
            targets=targets,
            reason="两个专科在同一方向上给出相反倾向，需检验依据是否充分",
            based_on=topic,
            kind="disagreement",
        ))
        if len(questions) >= MAX_CHALLENGES:
            return questions

    # ② 高影响不确定性：只挑"被 ≥2 个科室共同提及"的缺口（影响面大）
    if cards:
        gap_support: Dict[str, List[str]] = {}
        for card in cards:
            dept = getattr(card, "specialty_name", "")
            for gap in getattr(card, "information_gaps", []) or []:
                gap_support.setdefault(str(gap), [])
                if dept and dept not in gap_support[str(gap)]:
                    gap_support[str(gap)].append(dept)
        shared = [(g, d) for g, d in gap_support.items() if len(d) >= 2]
        for gap, depts in shared:
            if len(questions) >= MAX_CHALLENGES:
                break
            questions.append(ChallengeQuestion(
                question=f"「{gap}」目前仍缺失，而 {'、'.join(depts[:2])} 的判断都依赖它："
                         f"在缺少这项信息的情况下，当前结论是否还站得住？",
                targets=depts[:MAX_RESPONDERS],
                reason="多个专科的判断依赖同一项缺失信息",
                based_on=gap,
                kind="gap",
            ))

    return questions[:MAX_CHALLENGES]


# ---------------------------------------------------------------------------
# 2) 让相关专科补答一轮
# ---------------------------------------------------------------------------

def _build_respond_prompt(question: ChallengeQuestion, department: str,
                          text: str, previous_reason: str = "") -> str:
    return f"""【病例资料（已脱敏）】
{case_text_for_prompt(text)}

【你作为 {department} 的分析视角】
{previous_reason and f'你此前的判断要点：{previous_reason}'}

【来自质询视角的挑战】
{question.question}

【你的回应要求】
请针对上述挑战，选择以下**一种**立场并说明理由：
- 补充证据（我原来的判断需要补充哪些证据才能成立）
- 修正观点（我改变看法，理由是…）
- 维持原判断（理由…）

只输出 JSON：
{{"stance": "补充证据|修正观点|维持原判断",
  "reason": "具体理由（引用病例资料中的依据）",
  "follow_up_evidence": ["为验证此问题还需要补充的资料或检查"]}}

禁止输出任何置信度、概率或百分比数字。
"""


def _parse_response(raw: str) -> Dict[str, Any]:
    data, _ = loads_lenient(raw, fields=("stance", "reason", "follow_up_evidence"))
    if data is None:
        raise ValueError("未找到可解析的 JSON")
    stance = str(data.get("stance") or "").strip()
    if stance not in CHALLENGE_STANCES:
        stance = "维持原判断"
    follow_up = assert_safe_generated_list(
        [x for x in (data.get("follow_up_evidence") or []) if str(x).strip()],
        field_name="质询补充证据",
    )
    return {"stance": stance,
            "reason": assert_safe_generated_text(
                data.get("reason") or "", field_name="质询回应"),
            "follow_up_evidence": follow_up[:5]}


def _decide_outcome(responses: Sequence[Dict[str, str]], kind: str) -> str:
    """由回应推出质询结论（规则判定，不掺概率）。"""
    stances = [r.get("stance") for r in responses]
    if "修正观点" in stances:
        return "修正观点"
    if "补充证据" in stances:
        return "需补充证据"
    if kind == "disagreement":
        return "需医生裁决"     # 双方都维持 → 分歧未消解，交医生
    return "维持原判断"


def run(
    *,
    ai_service: Any,
    aggregation: Dict[str, Any],
    cards: Optional[Sequence[Any]] = None,
    text: str = "",
    card_reasons: Optional[Dict[str, str]] = None,
) -> ChallengeResult:
    """执行轻量质询：生成问题 → 相关科补答 → 汇总结论。

    无 LLM 时只产出问题清单（不假装有补答结果）。
    """
    result = ChallengeResult()
    questions = build_questions(aggregation, cards)
    result.questions = questions

    if not questions:
        result.skipped_reason = "本次聚合未发现分歧或共同缺口，无需质询"
        return result

    if ai_service is None or not getattr(ai_service, "api_key", ""):
        result.skipped_reason = "未配置模型，仅产出质询问题（未执行补答）"
        result.notes.append("质询问题已生成，等待模型可用后补答")
        return result

    card_reasons = card_reasons or {}
    for question in questions:
        responses: List[Dict[str, str]] = []
        follow_up: List[str] = []
        for dept in question.targets:
            prompt = _build_respond_prompt(question, dept, text,
                                           card_reasons.get(dept, ""))
            try:
                content, truncated, _error = call_structured(
                    ai_service, prompt,
                    role_description=CHALLENGE_SYSTEM_PROMPT.format(dept=dept),
                    max_tokens=CHALLENGE_MAX_TOKENS)
                if content:
                    parsed = _parse_response(content)
                    if truncated:
                        parsed["follow_up_evidence"] = list(parsed["follow_up_evidence"]) + [
                            "模型输出被长度截断，建议向医生确认是否需要补齐相关检查"]
                else:
                    parsed = {"stance": "维持原判断", "reason": "模型未返回内容",
                              "follow_up_evidence": []}
            except Exception as exc:  # pragma: no cover
                parsed = {"stance": "维持原判断", "reason": "回应失败：模型输出无法解析",
                          "follow_up_evidence": []}
            responses.append({"specialty": dept, "stance": parsed["stance"],
                              "reason": parsed["reason"]})
            follow_up.extend(parsed["follow_up_evidence"])

        outcome = _decide_outcome(responses, question.kind)
        round_ = ChallengeRound(
            question=question.question,
            raised_by=question.targets[0] if question.targets else "",
            responses=responses,
            outcome=outcome,
            follow_up_evidence=list(dict.fromkeys(follow_up))[:5],
        )
        round_.validate()          # 结构校验（枚举、非空）
        result.rounds.append(round_)

    result.method = METHOD_LLM
    if result.needs_doctor_decision:
        result.notes.append("质询后仍有未收敛的问题，已列入待医生裁决清单。")
    result.notes.append("质询只检验证据是否充分，不改变医生最终判断权。")
    return result
