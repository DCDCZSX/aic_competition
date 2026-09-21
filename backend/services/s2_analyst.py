"""S2 模型层：受限发言（规格书 §4.3 决策链第二段）。

与"让模型自由选科"完全不同——模型的上下文里**已经包含 S1 命中的科室及理由**：
- 指令：逐科说明"采纳 / 补充 / 反驳"；**反驳强规则命中必须给出医学依据**；
  只对自己新增的科室自报 0–1 置信度；
- **模型无权移除 strong 命中**（S3 会强制保留，这里只是记录它的反驳意见）；
- 失败即降级：无密钥 / 网络失败 / JSON 解析失败 → 返回空 picks，**分诊退回纯规则路径**（不影响可用性）。

从源头降低"模型与规则打架"的概率：模型看到规则后再发言。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ai_service import call_structured  # noqa: E402
from services.department_domain import ALL_NAMES, is_department  # noqa: E402
from services.json_recovery import loads_lenient  # noqa: E402
from services.rule_engine import RuleEvaluation  # noqa: E402

METHOD_LLM = "llm"
METHOD_SKIPPED = "skipped"          # 无可用模型
METHOD_FAILED = "failed"            # 调用/解析失败（已降级）

# 逐科表态的输出很短，2000 token 足够；万一被截断会如实写进降级原因
S2_MAX_TOKENS = 2000

STANCES = ("accept", "supplement", "reject")

SYSTEM_PROMPT = (
    "你是临床分诊协作助手，只做就医方向的组织与校验，不做诊断、不开处方、不给治疗方案。"
    "你的任务是对给定的候选科室逐科表态，输出严格的 JSON。"
)


@dataclass
class ModelPick:
    """模型对某个科室的表态。"""

    department: str
    stance: str = "accept"          # accept / supplement / reject
    confidence: Optional[float] = None
    reason: str = ""

    def to_orchestrator_pick(self) -> Dict[str, Any]:
        """转成 `triage.orchestrate(model_picks=...)` 需要的结构。"""
        data: Dict[str, Any] = {"department": self.department}
        if self.stance == "reject":
            data["stance"] = "reject"
            data["reason"] = self.reason
        else:
            if self.confidence is not None:
                data["confidence"] = self.confidence
            data["reason"] = self.reason
        return data


@dataclass
class S2Result:
    """S2 一轮发言的结果。"""

    picks: List[ModelPick] = field(default_factory=list)
    method: str = METHOD_SKIPPED
    error: str = ""
    raw: str = ""

    @property
    def orchestrator_picks(self) -> List[Dict[str, Any]]:
        return [p.to_orchestrator_pick() for p in self.picks]

    @property
    def rejected(self) -> List[ModelPick]:
        return [p for p in self.picks if p.stance == "reject"]

    def to_dict(self, include_raw: bool = False) -> Dict[str, Any]:
        data = {
            "method": self.method,
            "picks": [{"department": p.department, "stance": p.stance,
                       "confidence": p.confidence, "reason": p.reason} for p in self.picks],
            "error": self.error,
        }
        if include_raw:
            data["raw"] = self.raw
        return data


def _build_prompt(text: str, evaluation: RuleEvaluation, main_issue: str,
                  profile: Optional[Dict[str, Any]] = None) -> str:
    candidates = []
    for dep in evaluation.departments:
        score = evaluation.scores.get(dep, 0)
        weight = {3: "strong", 2: "medium", 1: "weak"}.get(score, "weak")
        signals = "；".join(evaluation.signals.get(dep, [])) or "（规则命中）"
        candidates.append(f"- {dep}（规则权重 {weight}，依据：{signals}）")
    candidate_block = "\n".join(candidates) if candidates else "- （规则未命中任何科室）"

    brief = ""
    if profile:
        parts = []
        if profile.get("age"):
            parts.append(f"{profile.get('age')}岁")
        if profile.get("sex"):
            parts.append(str(profile.get("sex")))
        if profile.get("chief_complaint"):
            parts.append(f"主诉：{profile['chief_complaint']}")
        if profile.get("information_gaps"):
            parts.append("信息缺口：" + "、".join(profile["information_gaps"][:4]))
        brief = "；".join(parts)

    return f"""【病例资料】
{text[:3000]}

【患者主要问题】{main_issue or '未提供'}
【病例摘要】{brief or '未提供'}

【规则引擎已选出的候选科室】
{candidate_block}

【你的任务】
1. 对上面每一个候选科室，逐科给出态度：accept（采纳）/ supplement（补充理由）/ reject（反驳）；
2. 如果你认为还需要其他科室参与，用 supplement 并给出该科室名与 0-1 之间的置信度；
3. **反驳规则命中的科室时，必须给出明确的医学依据**（哪条病史/检查提示不需要）；
4. 不要输出诊断结论、不要开药、不要给治疗方案；
5. 科室名必须来自下面这个合法列表（不得自造）：
{ '、'.join(ALL_NAMES) }

【输出格式】只输出 JSON，不要任何解释文字：
{{"picks": [{{"department": "内分泌科", "stance": "accept", "reason": "糖尿病5年为多病基础"}},
            {{"department": "眼科", "stance": "supplement", "confidence": 0.8, "reason": "视物模糊需眼底评估"}},
            {{"department": "皮肤科", "stance": "reject", "reason": "无皮疹等皮肤相关表现"}}]}}
"""


def _parse_picks(raw: str) -> List[ModelPick]:
    """从模型输出里解析 picks（容错解析：截断/全角标点/未转义引号都能恢复）。"""
    data, _ = loads_lenient(raw, fields=("picks",))
    if data is None:
        raise ValueError("输出中未找到可解析的 JSON")
    items = data.get("picks") or []
    if not isinstance(items, list):
        raise ValueError("picks 不是数组")

    picks: List[ModelPick] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        dep = str(item.get("department") or "").strip()
        if not dep:
            continue
        stance = str(item.get("stance") or "supplement").strip().lower()
        if stance not in STANCES:
            stance = "supplement"
        confidence = item.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None
        if confidence is not None:
            confidence = max(0.0, min(1.0, confidence))
        picks.append(ModelPick(department=dep, stance=stance,
                               confidence=confidence,
                               reason=str(item.get("reason") or "").strip()))
    return picks


def analyze(
    *,
    ai_service: Any,
    text: str,
    evaluation: RuleEvaluation,
    main_issue: str = "",
    profile: Optional[Dict[str, Any]] = None,
) -> S2Result:
    """执行一轮受限发言。任何失败都降级为"空 picks"，不影响规则路径。"""
    if ai_service is None or not getattr(ai_service, "api_key", ""):
        return S2Result(method=METHOD_SKIPPED, error="未配置模型（无 API Key），已退回纯规则分诊")

    prompt = _build_prompt(text, evaluation, main_issue, profile)
    content, truncated, error = call_structured(
        ai_service, prompt, role_description=SYSTEM_PROMPT, max_tokens=S2_MAX_TOKENS)

    if not content:
        detail = f"模型调用异常：{error}" if error else "模型未返回内容"
        return S2Result(method=METHOD_FAILED, error=f"{detail}，已退回纯规则分诊")

    try:
        picks = _parse_picks(content)
    except Exception as exc:
        hint = "（模型输出被长度截断）" if truncated else ""
        return S2Result(method=METHOD_FAILED,
                        error=f"模型输出解析失败{hint}：模型输出无法解析", raw=content[:500])

    # 域外科室过滤（不静默丢弃：记在 error 里说明）
    known, unknown = [], []
    for pick in picks:
        if is_department(pick.department):
            known.append(pick)
        else:
            unknown.append(pick.department)

    result = S2Result(picks=known, method=METHOD_LLM, raw=content[:500])
    if unknown:
        result.error = f"模型给出了域外科室，已忽略：{'、'.join(unknown)}"
    return result
