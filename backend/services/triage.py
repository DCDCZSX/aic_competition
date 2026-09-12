"""S3 安全约束与决策编排（确定性，无模型参与）。

对应《产品开发规格书-比赛版v-competition.md》§4.3 第三段。

**它只做编排，不裁定医学真相**：
- 入选规则（decision list，非加权和）：
  · 规则 strong 命中 → **必选**（模型不可剔除）
  · 规则 medium/weak → 入选（weak 标"可咨询"优先级）
  · 仅模型新增 → 当且仅当 `conf ≥ 0.65` 且通过校验器
- 校验器：科室∈域表；总数 ≤6；D 型强制含外科牵头科 + 麻醉/影像之一；牵头位=临床科
- 冲突不静默：模型明确反驳 strong 命中 → **保留该科** + 记 `conflict_log`，进报告分歧区
- 排序（仅 UI 用）：强规则 > 模型 conf≥0.8 > 中规则 > 弱规则/conf≥0.65
- 每病例记 `decision_path ∈ {rule-dominant, model-dominant, mixed, refused, degraded}`

红线：**任何分数不出现在用户可见 UI**（规格书 §4.3 打分口径）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import Reliability, reliability_level  # noqa: E402
from services.department_domain import (  # noqa: E402
    can_be_primary,
    group_of,
    is_auxiliary,
    is_department,
    to_code,
)
from services.rule_engine import (  # noqa: E402
    BEHAVIOR_D,
    WEIGHT_SCORE,
    RuleEvaluation,
    evaluate,
    infer_behavior,
    requires_mdt,
)

MODEL_CONF_THRESHOLD = 0.65      # 模型新增科室的最低置信
MODEL_HIGH_CONF = 0.80           # 排序用"高置信"阈值
MAX_DEPARTMENTS = 6
D_TYPE_SURGERY_REQUIRED = True   # D 型必须有外科牵头
# 参与科（辅助科）中 D 型需至少含其一
PERIOPERATIVE_SUPPORT = ("麻醉科", "影像科", "超声科", "介入科")


@dataclass
class DepartmentPick:
    """一个入选科室（含入选理由，供 UI 展示）。"""

    department: str
    role: str = "协同"                 # 牵头 / 协同 / 可咨询
    source: str = "rule"               # rule / model / rule+model
    score: int = 0                     # 规则权重分（**仅内部**）
    confidence: Optional[float] = None # 模型自报（**仅内部**）
    signals: List[str] = field(default_factory=list)
    reason: str = ""
    mandatory: bool = False            # 必选（不受数量上限裁剪，如 D 型的麻醉/影像）

    @property
    def code(self) -> Optional[str]:
        return to_code(self.department)

    def to_dict(self, include_scores: bool = False) -> Dict[str, Any]:
        """默认**不输出**任何分数（规格书 §4.3：分数不进 UI）。"""
        data = {
            "department": self.department,
            "code": self.code,
            "role": self.role,
            "signals": list(self.signals),
            "reason": self.reason or ("；".join(self.signals) if self.signals else ""),
        }
        if include_scores:
            data.update({"score": self.score, "confidence": self.confidence, "source": self.source})
        return data


@dataclass
class TriageOutcome:
    """分诊编排结果（对应患者端 P6 / 医生端屏 C）。"""

    case_id: str = ""
    behavior_type: str = "A"
    needs_mdt: bool = False
    primary: Optional[DepartmentPick] = None
    secondary: List[DepartmentPick] = field(default_factory=list)
    reliability: Optional[Reliability] = None
    decision_path: str = "rule-dominant"
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    needs_doctor_review: bool = True
    validator_notes: List[str] = field(default_factory=list)
    rule_hits: List[Dict[str, Any]] = field(default_factory=list)   # 可解释：命中了哪些信号

    @property
    def selected(self) -> List[str]:
        names = [self.primary.department] if self.primary else []
        names += [d.department for d in self.secondary]
        return names

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "behavior_type": self.behavior_type,
            "needs_mdt": self.needs_mdt,
            "primary": self.primary.to_dict() if self.primary else None,
            "secondary": [d.to_dict() for d in self.secondary],
            "selected": self.selected,
            "reliability": ({"level": self.reliability.level, "reasons": list(self.reliability.reasons)}
                            if self.reliability else None),
            "decision_path": self.decision_path,
            "conflicts": list(self.conflicts),
            "needs_doctor_review": self.needs_doctor_review,
            "validator_notes": list(self.validator_notes),
            "rule_hits": list(self.rule_hits),
        }


def _validate_department(name: str) -> bool:
    return is_department(name)


def orchestrate(
    *,
    text: str,
    case_id: str = "",
    evaluation: Optional[RuleEvaluation] = None,
    model_picks: Optional[Sequence[Dict[str, Any]]] = None,
    max_departments: int = MAX_DEPARTMENTS,
    completeness: float = 1.0,
    unresolved_gaps: int = 0,
    doctor_confirmed: bool = False,
) -> TriageOutcome:
    """执行 S3 编排。

    `model_picks`（S2 模型层输出，可选）：每项形如
        {"department": "眼科", "confidence": 0.8, "reason": "..."}          # 新增/补充
        {"department": "血液科", "stance": "reject", "reason": "..."}        # 反驳 strong 命中

    `completeness` / `unresolved_gaps`：来自画像与追问结果，用于**可靠性降级**
    （主文档 §5.5：跳过关键问题 → 降低证据支持等级，不得伪造完整信息）。
    """
    ev = evaluation or evaluate(text)
    behavior = ev.behavior_type or infer_behavior(text, ev)
    outcome = TriageOutcome(case_id=case_id, behavior_type=behavior,
                            needs_mdt=requires_mdt(behavior),
                            rule_hits=[h.to_dict() for h in ev.hits])

    notes: List[str] = []
    picks: Dict[str, DepartmentPick] = {}

    # ---- 1. 规则命中入选（strong 必选，medium/weak 入选）----
    for dep in ev.departments:
        score = ev.scores[dep]
        role = "协作" if score >= WEIGHT_SCORE["medium"] else "可咨询"
        picks[dep] = DepartmentPick(
            department=dep, role=role, source="rule", score=score,
            signals=list(ev.signals.get(dep, [])),
        )

    # ---- 2. 模型层：补充 / 反驳（反驳不生效，只记录）----
    model_only: List[str] = []
    for pick in model_picks or []:
        dep = str(pick.get("department") or "").strip()
        if not dep:
            continue
        if not _validate_department(dep):
            notes.append(f"模型给出的科室「{dep}」不在科室域内，已忽略")
            continue

        stance = str(pick.get("stance") or "").strip().lower()
        reason = str(pick.get("reason") or "").strip()

        if stance == "reject":
            # 冲突不静默：strong 命中保留，记 conflict_log（规格书 §4.3）
            if dep in picks and picks[dep].score == WEIGHT_SCORE["strong"]:
                outcome.conflicts.append({
                    "type": "model_rejected_strong_rule",
                    "department": dep,
                    "model_reason": reason,
                    "resolution": "保留该科（强规则命中不可被模型剔除），进入报告分歧区",
                })
                notes.append(f"模型反驳了强规则命中的「{dep}」：已保留并记入分歧")
            continue

        confidence = pick.get("confidence")
        confidence = float(confidence) if confidence is not None else None

        if dep in picks:
            # 采纳/补充：标记规则+模型双重支持
            picks[dep].source = "rule+model"
            if confidence is not None:
                picks[dep].confidence = confidence
            if reason and reason not in picks[dep].signals:
                picks[dep].signals.append(f"模型补充：{reason}")
            continue

        # 仅模型新增：需 conf ≥ 0.65 才入选
        if confidence is None or confidence < MODEL_CONF_THRESHOLD:
            notes.append(
                f"模型新增科室「{dep}」置信度不足（{confidence if confidence is not None else '未给出'} "
                f"< {MODEL_CONF_THRESHOLD}），未入选"
            )
            continue
        # 规则优先原则：规则候选已占满名额时，模型新增不再挤占（先规则，后模型补充）
        if len(picks) >= max_departments:
            notes.append(
                f"规则候选已达上限 {max_departments} 个，模型新增「{dep}」未入选（规则优先）"
            )
            continue
        picks[dep] = DepartmentPick(department=dep, role="可咨询", source="model",
                                   confidence=confidence,
                                   signals=[f"模型建议：{reason}"] if reason else ["模型建议"])
        model_only.append(dep)

    # ---- 2.5 D 型围术期补全（临床常规：手术必经麻醉评估；心脏手术需超声评估）----
    # 注意：这两类科室若**已由规则命中**，也要标记为必选，否则会被数量上限裁掉
    # （评测中实测到该缺陷：MDT-4 的超声科被裁）。
    if behavior == BEHAVIOR_D:
        if "麻醉科" in picks:
            picks["麻醉科"].mandatory = True
            picks["麻醉科"].signals.append("手术/介入决策 → 需麻醉评估（围术期常规，必选）")
        else:
            picks["麻醉科"] = DepartmentPick(
                department="麻醉科", role="协作", source="rule",
                score=WEIGHT_SCORE["medium"], mandatory=True,
                signals=["手术/介入决策 → 需麻醉评估（围术期常规参与科室）"],
            )
            notes.append("D 型病例自动纳入麻醉科（围术期常规，必选）")

        if any(k in (text or "") for k in ("心脏", "房间隔", "室间隔", "瓣膜", "搭桥", "心超", "冠脉")):
            if "超声科" in picks:
                picks["超声科"].mandatory = True
                picks["超声科"].signals.append("心脏结构/冠脉相关手术 → 需超声（心超）评估（必选）")
            else:
                picks["超声科"] = DepartmentPick(
                    department="超声科", role="协作", source="rule",
                    score=WEIGHT_SCORE["medium"], mandatory=True,
                    signals=["心脏结构/冠脉相关手术 → 需超声（心超）评估"],
                )
                notes.append("心脏相关手术自动纳入超声科（心超评估，必选）")

    # ---- 3. 数量控制（硬上限 ≤6；强规则之间也按优先级取舍）----
    if len(picks) > max_departments:
        def rank(dep: str):
            pick = picks[dep]
            lead = ev.lead_score(dep) if evaluation is not None else 1
            # 必选科室（D 型围术期）优先保留；其余按 规则权重 → 证据条数 → 牵头倾向 → 模型置信度
            return (not pick.mandatory, -pick.score, -len(pick.signals),
                    -lead, -(pick.confidence or 0))

        ordered_all = sorted(picks.keys(), key=rank)
        keep = set(ordered_all[:max_departments])
        dropped = [d for d in picks if d not in keep]
        for d in dropped:
            picks.pop(d, None)
        strong_dropped = [d for d in dropped
                          if ev.scores.get(d) == WEIGHT_SCORE["strong"]]
        notes.append(f"候选科室超过上限 {max_departments}，已按规则权重裁剪：{'、'.join(dropped)}")
        if strong_dropped:
            notes.append(f"其中 {len(strong_dropped)} 个强规则命中因数量上限被挤出，需人工确认是否补充")

    # ---- 4. 牵头科（仅限临床科；D 型要求外科牵头）----
    primary_name = _pick_primary(picks, behavior, ev)
    if primary_name:
        picks[primary_name].role = "牵头"
    else:
        notes.append("未能确定牵头科室（候选科室均为辅助科），已标记需人工确认")

    # ---- 5. D 型校验：必须含外科牵头 + 麻醉/影像之一 ----
    if behavior == BEHAVIOR_D:
        if not primary_name or group_of(primary_name) != "surgery":
            notes.append("D 型（手术/介入决策）应由外科牵头，当前牵头科不满足，需人工确认")
        if not any(s in picks for s in PERIOPERATIVE_SUPPORT):
            notes.append("D 型缺少围术期参与科室（麻醉/影像/超声/介入），建议补充")

    # ---- 6. 排序（仅 UI 用）与输出 ----
    ordered = sorted(
        picks.values(),
        key=lambda p: (p.role == "牵头", p.score, p.confidence or 0),
        reverse=True,
    )
    outcome.primary = next((p for p in ordered if p.role == "牵头"), None)
    outcome.secondary = [p for p in ordered if p.role != "牵头"]

    # ---- 7. 决策路径 ----
    if outcome.conflicts:
        outcome.decision_path = "mixed"
    elif model_only and not ev.hits:
        outcome.decision_path = "model-dominant"
    elif model_only:
        outcome.decision_path = "mixed"
    else:
        outcome.decision_path = "rule-dominant"

    # ---- 8. 可靠性等级（必带原因；模型自报置信度不参与）----
    has_conflict = bool(outcome.conflicts) or any("需人工确认" in n for n in notes)
    only_model = outcome.decision_path == "model-dominant"
    outcome.reliability = reliability_level(
        completeness=completeness if outcome.selected else 0.0,
        has_conflict=has_conflict,
        only_model_inference=only_model,
        multi_module_consistent=not outcome.conflicts,
        doctor_confirmed=doctor_confirmed,
    )
    # 未解决的高价值缺口 → 降一级（高→中，中→低），并**替换**成与等级一致的原因（主文档 §5.5）
    if unresolved_gaps > 0 and outcome.reliability.level != "低":
        downgraded = "中" if outcome.reliability.level == "高" else "低"
        base = [r for r in outcome.reliability.reasons
                if "完整、无冲突" not in r and "一致" not in r]
        outcome.reliability = Reliability(
            level=downgraded,
            reasons=[f"仍有 {unresolved_gaps} 项关键信息未确认，证据支持不足"] + base,
        )
    outcome.validator_notes = notes
    outcome.needs_doctor_review = True   # 比赛版一律建议医生复核（复杂病例）

    if not outcome.selected:
        outcome.decision_path = "degraded"
        outcome.validator_notes.append("未能给出任何科室建议，请补充资料或由医生人工判断")

    return outcome


def _pick_primary(picks: Dict[str, DepartmentPick], behavior: str,
                  evaluation: Optional[RuleEvaluation] = None) -> Optional[str]:
    """选择牵头科：临床科优先；D 型要求外科；同分时看科室牵头倾向。"""
    clinical = [d for d in picks if can_be_primary(d)]
    if not clinical:
        return None

    if behavior == BEHAVIOR_D:
        surgeons = [d for d in clinical if group_of(d) == "surgery"]
        if surgeons:
            clinical = surgeons

    def lead_of(dep: str) -> int:
        return evaluation.lead_score(dep) if evaluation is not None else 1

    # 规则分 → 牵头倾向 → 模型置信度
    clinical.sort(key=lambda d: (-picks[d].score, -lead_of(d), -(picks[d].confidence or 0)))
    return clinical[0]


def is_auxiliary_department(name: str) -> bool:
    """便捷导出：是否辅助科（可参与、不可牵头）。"""
    return is_auxiliary(name)
