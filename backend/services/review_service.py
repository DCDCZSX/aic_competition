"""医生端审核台服务（规格书 §5.6 / §7.1，任务 4.1-4.5）。

职责：
1. **枚举可审核的"决策条目"**（审核单位 = 决策条目，不是整份报告）：
   - `department_selection` 科室入选（牵头 / 协同）
   - `path_step`            就医路径步骤
   - `consensus_item`       共识条 / 分歧条
   - `patient_advice`       患者建议（就诊前准备、需向医生确认的问题）
2. **逐条审核**：接受 / 修改 / 驳回 / 暂不确定 / 请求补资料
   —— 修改与驳回**原因必填**、修改必须记录前后内容（`schemas.DoctorDecision` 已强制）；
3. **发布审核版**：所有条目终态后才可发布 → 病例状态 `doctor_confirmed`、报告版本 +1、角标转绿；
4. **审核记录与版本回溯**（屏 G）：条目状态 + 审计时间线 + 版本。

红线（写死在代码里）：
- 条目内容**不得**出现概率/置信度数字（模型自报数值不进 UI）；
- 条目文案**不得**出现指令式医疗行为（复用 `schemas` 的校验）；
- 医生操作**覆盖** AI 原稿而非并存两份草稿：`before` 永远保留 AI 原文，`after` 是医生改写，
  报告中使用 `after`，并在审核记录里留 `before`（可回溯）。

条目 id 采用 `"{target_type}:{target_id}"`，跨次调用稳定，便于决策覆盖与前端定位。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import DOCTOR_ACTIONS, DECISION_TARGET_TYPES, DoctorDecision, Reviewer  # noqa: E402

# 条目状态（界面层可见规则，对应 UI 设计说明 §4）
AWAITING = "awaiting"
CONFIRMED = "confirmed"
MODIFIED = "modified"
REJECTED = "rejected"
UNCERTAIN = "uncertain"

ITEM_STATES = (AWAITING, CONFIRMED, MODIFIED, REJECTED, UNCERTAIN)
ITEM_STATE_LABELS: Dict[str, str] = {
    AWAITING: "待审",
    CONFIRMED: "已接受",
    MODIFIED: "已修改",
    REJECTED: "已驳回",
    UNCERTAIN: "暂不确定",
}
TERMINAL_STATES = (CONFIRMED, MODIFIED, REJECTED, UNCERTAIN)

# 医生动作中文标签（审计与审核记录展示用）
ACTION_LABELS: Dict[str, str] = {
    "accept": "接受",
    "modify": "修改",
    "reject": "驳回",
    "uncertain": "暂不确定",
    "supplement_opinion": "录入线下意见",
    "request_more_info": "请求补资料",
}

# 审核对象类型中文标签（报告与审核记录对外展示用，避免暴露内部英文枚举）
TARGET_TYPE_LABELS: Dict[str, str] = {
    "department_selection": "科室入选",
    "path_step": "路径步骤",
    "consensus_item": "共识 / 分歧条目",
    "patient_advice": "患者建议",
}

# 医生动作 → 条目状态
ACTION_TO_STATE: Dict[str, str] = {
    "accept": CONFIRMED,
    "modify": MODIFIED,
    "reject": REJECTED,
    "uncertain": UNCERTAIN,
    "supplement_opinion": CONFIRMED,     # 录入线下意见：不改变条目，只登记医生意见
    "request_more_info": UNCERTAIN,      # 请求补资料：条目暂不确定 + 病例退回补资料
}

# 报告里"患者建议"类条目的来源段落（report_service 一页摘要的段落名）
ADVICE_SECTIONS = ("建议咨询方向", "需要向医生确认的问题")

# 红线：概率/置信度数字不得进入医生端 UI（模型自报数值同样不得展示）。
# 注意：**只**处理"概率/可能性 + 数字"这类表述，不动检验数值（如"空腹血糖 9.2"）与剂量，
# 否则会把真实医学数据篡改掉。
_FORBIDDEN_NUMERIC = re.compile(
    r"(概率|置信度|confidence|可能性)\s*[:：]?\s*(\d+(\.\d+)?\s*%?)",
    re.IGNORECASE,
)

# 报告里"（暂无）"类占位不进入审核条目（没有内容无从审核）
_PLACEHOLDER = re.compile(r"^[（(]\s*(暂无|未提供|暂未给出)")


class ReviewError(ValueError):
    """审核操作错误（条目不存在 / 非法动作 / 缺原因）。"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: Any) -> str:
    """去掉可能混入的概率/置信度表述（红线兜底），保留原意与检验数值。"""
    return _FORBIDDEN_NUMERIC.sub("（数值不展示）", str(text or ""))


@dataclass
class ReviewItem:
    """一条可审核的决策条目。"""

    item_id: str
    target_type: str
    target_id: str
    title: str
    ai_content: str                                  # AI 原稿（before）
    evidence_refs: List[str] = field(default_factory=list)
    reason_hint: str = ""                            # AI 给出的理由/依据（供医生判断）
    state: str = AWAITING
    reviewer_content: str = ""                       # 医生改写后的内容（after）
    decision_reason: str = ""                        # 医生填写的原因
    reviewer: str = ""                               # 谁审的
    decided_at: str = ""
    decision_id: str = ""
    needs_doctor_review: bool = True

    @property
    def final_content(self) -> str:
        """进入报告的内容：医生改过就用医生的，否则用 AI 原稿。"""
        if self.state in (MODIFIED,) and self.reviewer_content:
            return self.reviewer_content
        return self.ai_content

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def changed_by_doctor(self) -> bool:
        """是否被医生改写（报告里用 after、审核记录里保留 before）。"""
        return bool(self.state == MODIFIED and self.reviewer_content)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "title": _clean(self.title),
            "ai_content": _clean(self.ai_content),
            "final_content": _clean(self.final_content),
            "evidence_refs": list(self.evidence_refs),
            "reason_hint": _clean(self.reason_hint),
            "state": self.state,
            "state_label": ITEM_STATE_LABELS.get(self.state, self.state),
            "reviewer_content": _clean(self.reviewer_content),
            "decision_reason": _clean(self.decision_reason),
            "reviewer": self.reviewer,
            "decided_at": self.decided_at,
            "decision_id": self.decision_id,
            "changed_by_doctor": self.changed_by_doctor,
            "needs_doctor_review": self.needs_doctor_review,
        }


# ---------------------------------------------------------------------------
# 条目枚举（从病例 payload 派生，稳定 id）
# ---------------------------------------------------------------------------

def build_items(case: Any) -> List[ReviewItem]:
    """把一份病例展开成"可逐条审核"的决策条目列表。"""
    payload = dict((case or {}).get("payload") or {})
    case_id = str((case or {}).get("case_id") or payload.get("case_id") or "")
    items: List[ReviewItem] = []
    seen_contents: set = set()          # 去重：同一句话不重复成条（如 open_questions 同时出现在摘要里）

    def _dup(text: str) -> bool:
        key = re.sub(r"\s+", "", str(text or ""))
        if not key or key in seen_contents:
            return True
        seen_contents.add(key)
        return False

    # ---- ① 科室入选 ----
    triage = payload.get("triage") or {}
    primary = triage.get("primary") or {}
    if primary.get("department"):
        dept = primary["department"]
        items.append(_dept_item(case_id, dept, primary, role_label="牵头"))
    for pick in triage.get("secondary") or []:
        if pick.get("department"):
            items.append(_dept_item(case_id, pick["department"], pick, role_label=pick.get("role") or "协同"))

    # ---- ② 路径步骤 ----
    for step in (payload.get("path") or {}).get("steps") or []:
        step_id = str(step.get("step_id") or "")
        if not step_id:
            continue
        items.append(ReviewItem(
            item_id=f"path_step:{step_id}",
            target_type="path_step",
            target_id=step_id,
            title=f"就医路径：{step.get('title') or step_id}",
            ai_content=str(step.get("title") or ""),
            reason_hint=str(step.get("detail") or ""),
            evidence_refs=[],
            needs_doctor_review=bool(step.get("requires_doctor")),
        ))

    # ---- ③ 共识 / 分歧 ----
    mdt = payload.get("mdt") or {}
    agg = mdt.get("aggregation") or {}
    for idx, entry in enumerate(agg.get("consensus") or [], start=1):
        topic = str(entry.get("topic") or "")
        if not topic:
            continue
        if _dup(topic):
            continue
        refs = [f"专科意见：{'、'.join(entry.get('supporters') or [])}"]
        items.append(ReviewItem(
            item_id=f"consensus_item:C-{idx:02d}",
            target_type="consensus_item",
            target_id=f"C-{idx:02d}",
            title=f"多学科共识：{topic}",
            ai_content=str(entry.get("example") or topic),
            reason_hint="多科室在同一方向上一致（" + "、".join(entry.get("supporters") or []) + "）",
            evidence_refs=refs,
        ))
    for idx, entry in enumerate(agg.get("disagreements") or [], start=1):
        topic = str(entry.get("topic") or "")
        if not topic:
            continue
        # 与摘要"需要向医生确认的问题"里的同一条分歧去重（同一句话只审核一次）
        if _dup(f"分歧待裁决：{topic}"):
            continue
        positions = entry.get("positions") or {}
        detail = "；".join(f"{k}：{v}" for k, v in positions.items())
        items.append(ReviewItem(
            item_id=f"consensus_item:X-{idx:02d}",
            target_type="consensus_item",
            target_id=f"X-{idx:02d}",
            title=f"分歧待裁决：{topic}",
            ai_content=detail or topic,
            reason_hint=str(entry.get("resolving_info") or "需医生裁决"),
            evidence_refs=["证据状态：conflict"],
            needs_doctor_review=True,
        ))
    for idx, question in enumerate(agg.get("open_questions") or [], start=1):
        text = str(question)
        if not text or text.startswith("分歧待裁决：") or _dup(text):
            continue          # 分歧已单独成条，不重复
        items.append(ReviewItem(
            item_id=f"patient_advice:Q-{idx:02d}",
            target_type="patient_advice",
            target_id=f"Q-{idx:02d}",
            title="待医生确认问题",
            ai_content=text,
            reason_hint="由多学科分析聚合产出",
        ))

    # ---- ④ 患者建议（来自一页摘要的可审核段落）----
    try:
        from services import report_service
        summary = dict(getattr(report_service.build_patient_summary(dict(case or {})), "sections", {}) or {})
    except Exception:       # 报告不可用时降级：不因此丢掉其他条目
        summary = {}
    for section in ADVICE_SECTIONS:
        value = summary.get(section)
        entries = value if isinstance(value, list) else ([value] if value else [])
        for idx, text in enumerate(entries, start=1):
            text = str(text or "").strip()
            if not text or _PLACEHOLDER.match(text) or _dup(text):
                continue      # 占位（暂无/未提供）不进入审核条目
            items.append(ReviewItem(
                item_id=f"patient_advice:{section}-{idx:02d}",
                target_type="patient_advice",
                target_id=f"{section}-{idx:02d}",
                title=f"患者建议：{section}",
                ai_content=text,
                reason_hint="来自一页就医摘要",
            ))

    return items


def _dept_item(case_id: str, dept: str, pick: Dict[str, Any], *, role_label: str) -> ReviewItem:
    signals = [str(s) for s in (pick.get("signals") or [])]
    return ReviewItem(
        item_id=f"department_selection:{dept}",
        target_type="department_selection",
        target_id=dept,
        title=f"科室入选：{role_label} {dept}",
        ai_content=f"{role_label}建议：{dept}",
        reason_hint=str(pick.get("reason") or "；".join(signals)),
        evidence_refs=[f"命中信号：{s}" for s in signals],
        needs_doctor_review=True,
    )


# ---------------------------------------------------------------------------
# 决策覆盖
# ---------------------------------------------------------------------------

def latest_decisions(decisions: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按条目取**最后一次**医生操作（同一 id 多次操作以最新为准）。"""
    out: Dict[str, Dict[str, Any]] = {}
    for decision in decisions or []:
        key = f"{decision.get('target_type')}:{decision.get('target_id')}"
        out[key] = decision
    return out


def apply_decisions(items: Sequence[ReviewItem],
                    decisions: Sequence[Dict[str, Any]]) -> List[ReviewItem]:
    """把医生决策覆盖到条目上（就地更新并返回）。"""
    by_id = latest_decisions(decisions)
    for item in items:
        decision = by_id.get(item.item_id)
        if not decision:
            continue
        action = str(decision.get("action") or "")
        item.state = ACTION_TO_STATE.get(action, AWAITING)
        item.reviewer_content = str(decision.get("after") or "")
        item.decision_reason = str(decision.get("reason") or "")
        reviewer = decision.get("reviewer") or {}
        item.reviewer = str(reviewer.get("role") or "") or str(decision.get("reviewer_name") or "")
        item.decided_at = str(decision.get("created_at") or "")
        item.decision_id = str(decision.get("decision_id") or "")
        item.needs_doctor_review = item.state in (AWAITING, UNCERTAIN)
    return list(items)


def summarize(items: Sequence[ReviewItem]) -> Dict[str, Any]:
    """审核进度概览（屏 A 待审红点 / 屏 F 底部发布条件）。"""
    counts = {state: 0 for state in ITEM_STATES}
    for item in items or []:
        counts[item.state] = counts.get(item.state, 0) + 1
    total = len(items or [])
    pending = counts[AWAITING]
    human_touched = total - pending
    return {
        "total": total,
        "by_state": counts,
        "pending": pending,
        "human_touched": human_touched,
        "all_resolved": pending == 0,
        "can_publish": total > 0 and pending == 0,
        "progress": round(human_touched / total, 2) if total else 0.0,
    }


def make_decision(*, case_id: str, item: ReviewItem, action: str, after: str = "",
                  reason: str = "", reviewer_name: str = "审核医生",
                  reviewer_dept: str = "", version: int = 1,
                  decision_id: str = "") -> DoctorDecision:
    """构造并校验一条医生决策（原因必填等红线由 `DoctorDecision.validate` 强制）。"""
    if action not in DOCTOR_ACTIONS:
        raise ReviewError(f"未知审核动作：{action}（可选：{'、'.join(DOCTOR_ACTIONS)}）")
    if item.target_type not in DECISION_TARGET_TYPES:
        raise ReviewError(f"未知审核对象类型：{item.target_type}")
    if action == "modify" and not str(after or "").strip():
        raise ReviewError("「修改」必须填写修改后的内容")

    decision = DoctorDecision(
        decision_id=decision_id or f"{case_id}-{item.item_id}-{_now()}",
        case_id=case_id,
        target_type=item.target_type,
        target_id=item.target_id,
        action=action,
        before=item.ai_content,
        after=(after if action == "modify" else (after or item.final_content)),
        reason=reason,
        reviewer=Reviewer(role=reviewer_name or "审核医生", dept=reviewer_dept),
        evidence_refs=list(item.evidence_refs),
        version=int(version),
        created_at=_now(),
    )
    try:
        decision.validate()
    except ValueError as exc:
        # 转成审核语义的错误信息（前端直接展示）
        raise ReviewError(str(exc)) from exc
    return decision


def find_item(items: Sequence[ReviewItem], item_id: str) -> ReviewItem:
    for item in items or []:
        if item.item_id == str(item_id):
            return item
    raise ReviewError(f"审核条目不存在：{item_id}")


def pending_questions(items: Sequence[ReviewItem]) -> List[str]:
    """"暂不确定"的条目进入报告的待医生确认问题清单（屏 F 规则）。

    带上医生当时写的"为什么不确定"，报告里才能直接转成线下要问的问题。
    """
    out: List[str] = []
    for item in items or []:
        if item.state == UNCERTAIN:
            text = f"{item.title}：{item.final_content}"
            if item.decision_reason:
                text += f"（原因：{item.decision_reason}）"
            out.append(text)
    return out
