"""医生端审核台服务（规格书 §5.6 / §7.1，任务 4.1-4.5）。

职责：
1. **枚举可审核的"医学决策条目"**（审核单位 = 决策条目，不是整份报告）：
   - `path_step`            需要医生决定的医疗安排；普通路径步骤自动处理
   - `consensus_item`       MDT 共识抽查 / 未解决分歧必审
   - 分流科室和患者沟通内容分别由分流确认、报告预览负责，不在此重复审核
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
from services.model_output_guard import public_text  # noqa: E402
from services.doctor_mdt import review_analysis, review_mdt, analysis_source  # noqa: E402

# 条目状态（界面层可见规则，对应 UI 设计说明 §4）
AWAITING = "awaiting"
CONFIRMED = "confirmed"
MODIFIED = "modified"
REJECTED = "rejected"
UNCERTAIN = "uncertain"
# 派生态（**不落库**）：规则判定"不需要医生确认"的条目（如未要求医生决定的路径步骤）。
# 与"医生已接受"必须严格区分 —— 报告、审计都不能把它写成医生确认过（红线）。
AUTO_PROCESSED = "auto_processed"
# 派生态（**不落库**）：分歧已由医生在 MDT 页“分歧的医生处理”中记录结果，报告审核里不再重复裁决。
# 与 AUTO_PROCESSED 不同：这是医生本人处理过的，只是记录在分歧处理表单而不是审核卡上。
HANDLED_IN_MDT = "handled_in_mdt"

# 审核责任分级：只有明确不需要医生决定的内容才自动归档；普通摘要可抽查，
# 分歧、风险、资料缺口和医疗安排仍保持必审，并可在后续标记中自动升级。
REQUIRED = "required"
SAMPLE = "sample"
AUTOMATIC = "automatic"
REVIEW_TIERS = (REQUIRED, SAMPLE, AUTOMATIC)
REVIEW_TIER_LABELS: Dict[str, str] = {
    REQUIRED: "必审",
    SAMPLE: "建议抽查",
    AUTOMATIC: "自动处理",
}
UNKNOWN_TIER_REASON = "未匹配已审核的分级规则，默认必审"
REQUIRED_GENERIC_REASON = "该条目需要医生确认"

ITEM_STATES = (AWAITING, CONFIRMED, MODIFIED, REJECTED, UNCERTAIN, AUTO_PROCESSED, HANDLED_IN_MDT)
ITEM_STATE_LABELS: Dict[str, str] = {
    AWAITING: "待审",
    CONFIRMED: "已接受",
    MODIFIED: "已修改",
    REJECTED: "已驳回",
    UNCERTAIN: "暂不确定",
    AUTO_PROCESSED: "免审（自动）",
    HANDLED_IN_MDT: "已在 MDT 处理",
}
AUTO_PROCESSED_REASON = "规则判定免审：该路径步骤未要求医生决定（医生可随时接管）"
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
    "ai_analysis": "AI 专科意见",
    "path_step": "医疗安排",
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

class ReviewError(ValueError):
    """审核操作错误（条目不存在 / 非法动作 / 缺原因）。"""


DISAGREEMENT_STATUSES = ("resolved", "unresolved_with_followup", "need_more_info")
DISAGREEMENT_STATUS_LABELS: Dict[str, str] = {
    "resolved": "已解决",
    "unresolved_with_followup": "未解决，需后续跟进",
    "need_more_info": "需要补充资料",
}


def disagreement_status_label(status: Any) -> str:
    return DISAGREEMENT_STATUS_LABELS.get(str(status or ""), "待牵头医生处理")


def latest_disagreement_decisions(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按分歧编号保留最后一次牵头医生处理决定。"""
    out: Dict[str, Dict[str, Any]] = {}
    for record in records or []:
        disagreement_id = str(record.get("disagreement_id") or "").strip()
        if disagreement_id:
            out[disagreement_id] = dict(record)
    return out


def make_disagreement_decision(*, disagreement_id: str, topic: str, status: str,
                               choice: str = "", reason: str = "",
                               next_step: str = "", reviewer_name: str = "",
                               reviewer_dept: str = "", decided_at: str = "",
                               content_revision: int = 1) -> Dict[str, Any]:
    """校验并构造独立于 AI 聚合结果的分歧处理记录。"""
    safe_id = str(disagreement_id or "").strip()
    safe_topic = str(topic or "").strip()
    safe_status = str(status or "").strip()
    safe_choice = str(choice or "").strip()
    safe_reason = str(reason or "").strip()
    safe_next_step = str(next_step or "").strip()
    safe_reviewer = str(reviewer_name or "").strip()
    if not safe_id or not safe_topic:
        raise ReviewError("分歧编号和主题不能为空")
    if safe_status not in DISAGREEMENT_STATUSES:
        raise ReviewError("不支持的分歧处理状态")
    if not safe_reason:
        raise ReviewError("分歧处理理由不能为空")
    if safe_status == "resolved" and not safe_choice:
        raise ReviewError("已解决的分歧必须填写牵头医生决定或修改结果")
    if safe_status != "resolved" and not safe_next_step:
        raise ReviewError("未解决分歧必须填写下一步处理方式")
    if not safe_reviewer:
        raise ReviewError("责任审核人不能为空")
    try:
        revision = max(1, int(content_revision or 1))
    except (TypeError, ValueError):
        revision = 1
    return {
        "disagreement_id": safe_id,
        "topic": safe_topic,
        "status": safe_status,
        "status_label": disagreement_status_label(safe_status),
        "choice": safe_choice,
        "resolution": safe_choice,
        "reason": safe_reason,
        "next_step": safe_next_step,
        "reviewer_name": safe_reviewer,
        "reviewer_dept": str(reviewer_dept or "").strip(),
        "decided_at": decided_at or _now(),
        "content_revision": revision,
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: Any) -> str:
    """去掉可能混入的概率/置信度表述（红线兜底），保留原意与检验数值。"""
    return public_text(text)


def specialty_analysis_summary(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """将一条专科分析压缩成一张可审核的整体意见卡。

    医生审核的是一个科室对本病例的整体意见；事实、判断、待确认事项
    和缺口留在卡片上下文中，不拆成几十条互相重复的审核项。
    """
    analysis = analysis or {}
    department = str(analysis.get("department") or "未知科室").strip()
    status = str(analysis.get("status") or "ok").strip().lower()
    inferences = []
    for item in (analysis.get("inferences") or [])[:4]:
        text = item.get("text") if isinstance(item, dict) else item
        if str(text or "").strip():
            inferences.append(_clean(text))
    checks = []
    for item in (analysis.get("checks") or [])[:5]:
        text = item.get("content") if isinstance(item, dict) else item
        if str(text or "").strip():
            checks.append(_clean(text))
    gaps = [_clean(value) for value in (analysis.get("information_gaps") or [])[:6]
            if str(value or "").strip()]
    facts = [_clean(value) for value in (analysis.get("facts") or [])[:6]
             if str(value or "").strip()]
    focus = _clean(analysis.get("focus") or "")
    referral = _clean(analysis.get("referral") or "")
    refs = []
    for item in (analysis.get("inferences") or []):
        if not isinstance(item, dict):
            continue
        refs.extend(str(ref).strip() for ref in (item.get("evidence_refs") or [])
                    if str(ref).strip())
    if status != "ok":
        content = f"{department}本次专科 AI 意见未完成，不能按已生成意见发布。"
    else:
        parts = []
        if focus:
            parts.append(f"关注重点：{focus}")
        if inferences:
            parts.append("AI 评估：" + "；".join(inferences))
        if checks:
            parts.append("待确认事项：" + "；".join(checks))
        if gaps:
            parts.append("该科信息缺口：" + "；".join(gaps))
        if referral:
            parts.append("协作方向：" + referral)
        content = "\n".join(parts) or f"{department}本次未提供可审核的专科意见。"
    return {
        "department": department,
        "status": status or "ok",
        "content": _clean(content),
        "facts": facts,
        "inferences": inferences,
        "checks": checks,
        "information_gaps": gaps,
        "referral": referral,
        "evidence_refs": list(dict.fromkeys(refs)),
    }


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
    auto_reason: str = ""                            # 规则判定免审时的原因（仅派生态有值）
    review_tier: str = REQUIRED
    tier_reason: str = ""
    high_risk: bool = False
    conflict: bool = False
    analysis_failed: bool = False
    # 同一稳定 item_id 的内容发生变化时递增；旧决定仍保留但不再覆盖新内容。
    content_revision: int = 1
    department: str = ""
    source_label: str = ""
    # 分歧已在 MDT 页记录医生处理结果时，这里是处理摘要；非空表示报告审核不再重复裁决。
    handled_in_mdt: str = ""

    def __post_init__(self) -> None:
        """规范化分级；未知值必须回到必审，避免静默放行。"""
        try:
            self.content_revision = max(1, int(self.content_revision or 1))
        except (TypeError, ValueError):
            self.content_revision = 1
        tier = str(self.review_tier or REQUIRED)
        if tier not in REVIEW_TIERS:
            tier = REQUIRED
            self.tier_reason = self.tier_reason or UNKNOWN_TIER_REASON
        self.review_tier = tier
        if not self.tier_reason:
            self.tier_reason = (
                AUTO_PROCESSED_REASON if tier == AUTOMATIC else
                "经审核的抽查规则" if tier == SAMPLE else
                REQUIRED_GENERIC_REASON
            )
        if tier != REQUIRED and (self.high_risk or self.conflict or self.analysis_failed):
            tier = REQUIRED
            self.review_tier = tier
            self.tier_reason = "存在风险、冲突或分析失败，必须由医生处理"
        # 分级是审核责任的单一来源；保留 needs_doctor_review 作为旧接口兼容字段。
        self.needs_doctor_review = tier == REQUIRED

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

    @property
    def review_tier_label(self) -> str:
        return REVIEW_TIER_LABELS.get(self.review_tier, REVIEW_TIER_LABELS[REQUIRED])

    @property
    def batch_block_reason(self) -> str:
        """返回不能被批量接受的原因；空字符串表示可批量处理。"""
        if self.review_tier != REQUIRED:
            return ""
        if self.high_risk:
            return "高风险条目必须逐条处理"
        if self.conflict:
            return "冲突或分歧条目必须逐条处理"
        if self.analysis_failed:
            return "分析失败条目必须逐条处理"
        if self.target_type == "ai_analysis":
            return "AI 关键判断必须逐条确认"
        if self.target_type == "path_step":
            return "关键路径步骤必须逐条确认"
        return ""

    @property
    def batch_eligible(self) -> bool:
        return not self.batch_block_reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "department": self.department,
            "source_label": self.source_label,
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
            "auto_reason": _clean(self.auto_reason),
            "review_tier": self.review_tier,
            "review_tier_label": self.review_tier_label,
            "tier_reason": _clean(self.tier_reason),
            "high_risk": self.high_risk,
            "conflict": self.conflict,
            "analysis_failed": self.analysis_failed,
            "content_revision": self.content_revision,
            "batch_eligible": self.batch_eligible,
            "batch_block_reason": _clean(self.batch_block_reason),
            "handled_in_mdt": bool(self.handled_in_mdt),
            "handled_note": _clean(self.handled_in_mdt),
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

    # ---- ① 专科整体意见（医生审核的主对象） ----
    # 一个科室只生成一张卡；事实、判断、待确认项和缺口收敛在卡片上下文中，
    # 避免把同一份专科意见拆成几十个无法独立判断的审核条目。
    analysis_view = review_analysis(payload)
    source = analysis_source(payload)
    analyses = list(analysis_view.get("analyses") or [])
    seen_departments: Dict[str, int] = {}
    for analysis in analyses:
        summary = specialty_analysis_summary(analysis)
        department = summary["department"]
        seen_departments[department] = seen_departments.get(department, 0) + 1
        occurrence = seen_departments[department]
        target_id = department if occurrence == 1 else f"{department}#{occurrence}"
        item = ReviewItem(
            item_id=f"ai_analysis:{target_id}",
            target_type="ai_analysis",
            target_id=target_id,
            title=f"AI 专科意见：{department}",
            ai_content=summary["content"],
            reason_hint=(f"审核 {department} 对本病例的整体判断；"
                         "请结合原始资料决定是否采纳、修改或驳回"),
            evidence_refs=summary["evidence_refs"],
            needs_doctor_review=True,
            review_tier=REQUIRED,
            tier_reason="AI 专科意见必须由医生逐科确认",
            analysis_failed=summary["status"] != "ok",
            department=department,
            source_label=("本次 AI MDT 模拟" if source == "simulation"
                          else "提交阶段 AI 专科分析"),
        )
        items.append(item)

    # 若分析服务只返回失败科室清单而没有占位分析，也要留下可处理的失败卡，
    # 不能因为结果为空就静默放行。
    failed_departments = analysis_view.get("failed") or analysis_view.get("failed_departments") or []
    for department in failed_departments:
        department = str(department or "").strip()
        if not department or department in seen_departments:
            continue
        seen_departments[department] = 1
        items.append(ReviewItem(
            item_id=f"ai_analysis:{department}", target_type="ai_analysis",
            target_id=department, title=f"AI 专科意见：{department}",
            ai_content=f"{department}本次专科 AI 意见未完成，不能按已生成意见发布。",
            reason_hint="分析失败，需医生决定是否补充资料或重新运行分析",
            needs_doctor_review=True, review_tier=REQUIRED,
            tier_reason="分析失败条目必须逐条处理", analysis_failed=True,
            department=department,
            source_label=("本次 AI MDT 模拟" if source == "simulation"
                          else "提交阶段 AI 专科分析"),
        ))

    # ---- ② 路径步骤（保留为发布门槛/上下文） ----
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
            review_tier=REQUIRED if step.get("requires_doctor") else AUTOMATIC,
            tier_reason=("该路径步骤明确要求医生处理"
                         if step.get("requires_doctor") else AUTO_PROCESSED_REASON),
        ))

    # ---- ③ 共识 / 分歧（保留为发布门槛/上下文） ----
    mdt = review_mdt(payload)
    source_label = "本次 AI MDT 模拟" if analysis_source(payload) == "simulation" else "提交阶段专科 AI 分析"
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
            review_tier=SAMPLE,
            tier_reason="与上方 MDT 结果一致，建议抽查，不阻塞发布",
            source_label=source_label,
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
            review_tier=REQUIRED,
            tier_reason="存在未解决分歧：请在“MDT 协作与会诊记录”页的“分歧的医生处理”中记录处理结果",
            conflict=True,
        ))
    _annotate_item_flags(items, payload)
    revisions = payload.get("review_item_revisions") or {}
    for item in items:
        try:
            item.content_revision = max(1, int(revisions.get(item.item_id, 1) or 1))
        except (TypeError, ValueError):
            item.content_revision = 1
        if item.target_type == "consensus_item" and analysis_source(payload) == "simulation":
            item.content_revision = max(item.content_revision,
                int((payload.get("doctor_mdt_simulation") or {}).get("content_revision") or 1))
    _mark_disagreements_handled_in_mdt(items, payload)
    return items


def _is_disagreement_item(item: "ReviewItem") -> bool:
    return item.target_type == "consensus_item" and str(item.target_id).startswith("X-")


def _mark_disagreements_handled_in_mdt(items: Sequence["ReviewItem"], payload: Dict[str, Any]) -> None:
    """分歧只在 MDT 页“分歧的医生处理”中裁决一次。

    当前修订已有医生处理记录的分歧，在报告审核里标为“已在 MDT 处理”，不再要求重复点一次
    接受/修改；处理记录过期（分歧内容变化）或尚未记录时，仍保持必审并提示去 MDT 页记录。
    """
    records = latest_disagreement_decisions(
        current_disagreement_decisions(items, payload.get("disagreement_decisions") or []))
    for item in items:
        if not _is_disagreement_item(item):
            continue
        record = records.get(item.target_id) or {}
        status = str(record.get("status") or "")
        if status not in DISAGREEMENT_STATUSES:
            continue
        detail = str(record.get("resolution") or record.get("choice") or record.get("next_step") or "").strip()
        who = str(record.get("reviewer_name") or "").strip()
        note = f"医生已在 MDT 页记录：{disagreement_status_label(status)}"
        if detail:
            note += f"——{detail}"
        if who:
            note += f"（{who}）"
        item.handled_in_mdt = note


def _is_failed_state(value: Any) -> bool:
    return str(value or "").strip().lower() in {"failed", "partial_failed", "error"}


def _annotate_item_flags(items: Sequence[ReviewItem], payload: Dict[str, Any]) -> None:
    """把病例级风险、冲突和分析失败标记映射到审核条目。

    这里只使用可解释的结构化字段，不使用模型置信度作为免审依据。
    """
    triage = payload.get("triage") or {}
    mdt = review_mdt(payload)
    aggregation = mdt.get("aggregation") or {}
    reliability = (triage.get("reliability") or {}).get("level")
    high_risk = bool(
        triage.get("high_risk") or triage.get("risk_flags") or
        payload.get("high_risk") or payload.get("risk_flags") or
        triage.get("risk_level") in ("高", "high") or
        payload.get("risk_level") in ("高", "high") or reliability == "低"
    )

    conflict_departments = {
        str(entry.get("department") or "")
        for entry in (triage.get("conflicts") or [])
        if entry.get("department")
    }
    for entry in aggregation.get("disagreements") or []:
        conflict_departments.update(str(dep) for dep in (entry.get("positions") or {}) if dep)

    analysis = payload.get("analysis") or {}
    analysis_mdt = analysis.get("mdt") or {}
    gaps = [str(gap).strip() for gap in (payload.get("gaps") or []) if str(gap).strip()]
    phase_states = [
        (phase or {}).get("state")
        for phase in (analysis.get("phases") or {}).values()
    ]
    states = [analysis.get("state"), *phase_states, analysis_mdt.get("state"), mdt.get("state")]
    failed_departments = {
        str(entry.get("department") or "")
        for entry in (mdt.get("analyses") or [])
        if entry.get("department") and entry.get("status") not in (None, "ok")
    }
    failed_departments.update(
        str(department) for department in (analysis_mdt.get("failed") or []) if str(department).strip()
    )
    failed_departments.update(
        item.department for item in items
        if item.target_type == "ai_analysis" and item.analysis_failed and item.department
    )
    # “部分失败”只说明有科室没跑完：能定位到具体科室时只标记那几个科室的卡片，
    # 其余科室已生成的意见不再显示“分析失败”。整体失败、或部分失败却定位不到科室时，
    # 仍按整体失败处理（保守）。
    whole_analysis_failed = any(str(state or "").strip().lower() in {"failed", "error"} for state in states) or (
        any(str(state or "").strip().lower() == "partial_failed" for state in states) and not failed_departments
    )

    for item in items:
        # 共识条目是各科一致的内容，病例整体高风险或某科分析未完成都不改变“建议抽查”；
        # 只有这条共识本身对应到资料缺口（见下方 gap_match）时才升级为必审。
        is_consensus = item.target_type == "consensus_item" and not _is_disagreement_item(item)
        item.high_risk = bool(item.high_risk or (high_risk and not is_consensus))
        item.conflict = bool(
            item.conflict or
            (item.target_type == "consensus_item" and "分歧" in item.title)
        )
        # 专科卡片只看本科室是否失败；就医路径步骤沿用原来的保守口径：任何科室未完成都需逐条处理。
        if item.target_type == "ai_analysis":
            failed_here = item.department in failed_departments or whole_analysis_failed
        elif item.target_type == "path_step":
            failed_here = whole_analysis_failed or bool(failed_departments)
        else:
            failed_here = whole_analysis_failed and not is_consensus and item.target_type == "consensus_item"
        item.analysis_failed = bool(item.analysis_failed or failed_here)

        item_text = " ".join((item.title, item.ai_content, item.reason_hint))
        gap_match = False
        for gap in gaps:
            label = re.split(r"[:：]", gap, maxsplit=1)[0].strip()
            # 只有能对应到当前医学条目文本的缺口才升级；无法定位的病例级缺口
            # 由分流/MDT 页面展示，不把无关审核项批量升级。
            if label and label in item_text:
                gap_match = True
                break

        critical_reason = ""
        if item.high_risk:
            critical_reason = "存在高风险标记，必须由医生处理"
        elif item.conflict:
            critical_reason = "存在冲突或分歧，必须由医生处理"
        elif item.analysis_failed:
            critical_reason = "相关分析未完成，必须由医生处理"
        elif gap_match:
            critical_reason = "存在资料缺口，必须由医生处理"
        if critical_reason and item.review_tier != REQUIRED:
            item.review_tier = REQUIRED
            item.needs_doctor_review = True
            item.tier_reason = critical_reason


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


def _decision_revision(decision: Dict[str, Any]) -> int:
    """读取决定绑定的内容修订号；旧决定缺字段时兼容为第 1 版。"""
    try:
        return max(1, int(decision.get("content_revision") or 1))
    except (TypeError, ValueError):
        return 1


def current_decisions(items: Sequence[ReviewItem],
                      decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """报告只应用绑定当前条目修订的决定；历史仍保留在存储中。"""
    revisions = {item.item_id: item.content_revision for item in items or []}
    return [record for record in decisions or []
            if revisions.get(f"{record.get('target_type')}:{record.get('target_id')}")
            == _decision_revision(record)]


def current_disagreement_decisions(items: Sequence[ReviewItem],
                                   records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同名分歧在新模拟中重新出现时，不沿用上一修订的处理内容。"""
    revisions = {item.item_id: item.content_revision for item in items or []}
    return [record for record in records or []
            if revisions.get(f"consensus_item:{record.get('disagreement_id')}")
            == _decision_revision(record)]


def stale_decisions(items: Sequence[ReviewItem],
                    decisions: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """返回不再针对当前审核项内容的历史决定。"""
    current = {item.item_id: item.content_revision for item in items or []}
    out: List[Dict[str, Any]] = []
    for decision in decisions or []:
        item_id = f"{decision.get('target_type')}:{decision.get('target_id')}"
        expected = current.get(item_id)
        if expected is None or _decision_revision(decision) != expected:
            row = dict(decision)
            row["item_id"] = item_id
            row["content_revision"] = _decision_revision(decision)
            row["current_content_revision"] = expected
            row["stale"] = True
            out.append(row)
    return out


def affected_item_ids(old_items: Sequence[ReviewItem], new_items: Sequence[ReviewItem],
                      affected_departments: Sequence[str] = (),
                      update_content: str = "") -> List[str]:
    """按稳定条目 id 找出资料更新后需要重新确认的审核项。

    路径和共识项按本次资料中的可见主题定位。条目内容本身发生变化、或新出现的条目也会被
    纳入，未命中的现有条目保持原决定。
    """
    old_map = {item.item_id: item for item in old_items or []}
    new_map = {item.item_id: item for item in new_items or []}
    departments = {str(dep).strip() for dep in affected_departments or [] if str(dep).strip()}
    update_text = str(update_content or "").strip()
    tokens = [dep for dep in departments if len(dep) >= 2]
    # 中文更新没有可靠的空格分词，保留两个字以上的连续片段作为主题线索。
    tokens.extend(token for token in re.findall(r"[\u4e00-\u9fff]{2,}", update_text)
                  if token not in tokens)

    out = set()
    for item_id, item in new_map.items():
        old = old_map.get(item_id)
        text = " ".join((item.title, item.ai_content, item.reason_hint,
                           *[str(ref) for ref in item.evidence_refs]))
        topic_match = any(token in text for token in tokens)
        content_changed = bool(old and (
            old.ai_content != item.ai_content or old.reason_hint != item.reason_hint
            or old.title != item.title or old.evidence_refs != item.evidence_refs
        ))
        if item_id not in old_map or topic_match or content_changed:
            out.add(item_id)

    return sorted(out)


def bump_content_revisions(revisions: Optional[Dict[str, Any]],
                           item_ids: Sequence[str] = ()) -> Dict[str, int]:
    """Advance the current content revision for affected review items.

    Historical decisions stay in storage; ``apply_decisions`` ignores them
    once their content revision no longer matches the current item.  Missing
    or malformed legacy values start at revision one.
    """
    updated: Dict[str, int] = {}
    for raw_item_id in item_ids or ():
        item_id = str(raw_item_id or "").strip()
        if not item_id:
            continue
        try:
            current = max(1, int((revisions or {}).get(item_id, 1) or 1))
        except (TypeError, ValueError):
            current = 1
        updated[item_id] = current + 1
    return updated


def apply_decisions(items: Sequence[ReviewItem],
                    decisions: Sequence[Dict[str, Any]]) -> List[ReviewItem]:
    """把医生决策覆盖到条目上（就地更新并返回），并派生"免审"状态。

    两条规则（2026-09-17 修正，此前有缺陷）：
    1. `needs_doctor_review` 由**条目自身的业务语义**决定（如路径步骤是否要求医生决定），
       不能被"是否还没审"覆盖 —— 旧实现用 `state in (AWAITING, UNCERTAIN)` 覆盖了它，
       导致"理论上免审"的条目照样计入待审、照样阻塞发布；
    2. 医生没干预过、且规则判定免审的条目 → 派生为 `auto_processed`（**不落库、不计入医生处理**）。
    """
    current_revisions = {item.item_id: item.content_revision for item in items or []}
    # 只在同一内容修订内取最后一条决定。这样即使历史数据的写入顺序异常，
    # 当前修订的有效决定也不会被较晚写入的过期决定遮掉。
    by_id: Dict[str, Dict[str, Any]] = {}
    for decision in decisions or []:
        item_id = f"{decision.get('target_type')}:{decision.get('target_id')}"
        if item_id not in current_revisions:
            continue
        if _decision_revision(decision) != current_revisions[item_id]:
            continue
        by_id[item_id] = decision
    for item in items:
        item.auto_reason = ""
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
    for item in items:
        has_decision = item.item_id in by_id
        if item.handled_in_mdt and not has_decision:
            item.state = HANDLED_IN_MDT
        elif item.review_tier == AUTOMATIC and not has_decision:
            item.state = AUTO_PROCESSED
            item.auto_reason = item.tier_reason or AUTO_PROCESSED_REASON
        elif item.review_tier in (REQUIRED, SAMPLE) and not has_decision:
            item.state = AWAITING
    return list(items)


def summarize(items: Sequence[ReviewItem]) -> Dict[str, Any]:
    """审核进度概览（待审红点 / 底部发布条件）。

    `pending` 只统计**必审且未处理**的条目：建议抽查和自动处理不会阻塞发布，
    但建议抽查仍单独显示为待抽查。
    """
    counts = {state: 0 for state in ITEM_STATES}
    for item in items or []:
        counts[item.state] = counts.get(item.state, 0) + 1
    total = len(items or [])
    tier_counts = {
        tier: sum(1 for item in items or [] if item.review_tier == tier)
        for tier in REVIEW_TIERS
    }
    pending = sum(1 for i in items or []
                  if i.review_tier == REQUIRED and i.state == AWAITING)
    sample_pending = sum(1 for i in items or []
                         if i.review_tier == SAMPLE and i.state == AWAITING)
    # "医生已处理" = 医生这次决定**仍然生效**（终态）；被撤销回到待审的条目不能既算待审又算已处理
    doctor_reviewed = sum(1 for i in items or []
                          if i.decision_id and i.state in TERMINAL_STATES)
    auto_processed = counts.get(AUTO_PROCESSED, 0)
    high_risk = sum(1 for i in items or [] if i.high_risk)
    conflict = sum(1 for i in items or [] if i.conflict)
    analysis_failed = sum(1 for i in items or [] if i.analysis_failed)
    specialty_items = [i for i in items or [] if i.target_type == "ai_analysis"]
    specialty_pending = sum(1 for i in specialty_items
                            if i.review_tier == REQUIRED and i.state == AWAITING)
    specialty_reviewed = sum(1 for i in specialty_items
                             if i.decision_id and i.state in TERMINAL_STATES)
    specialty_failed = sum(1 for i in specialty_items if i.analysis_failed)
    batch_blocked = sum(1 for i in items or []
                        if i.review_tier == REQUIRED and not i.batch_eligible)
    batch_blocked_pending = sum(1 for i in items or []
                                if i.review_tier == REQUIRED and i.state == AWAITING
                                and not i.batch_eligible)
    blocking: List[str] = []
    if total == 0:
        blocking.append("该病例暂无决策条目")
    if pending:
        blocking.append(f"仍有 {pending} 条必审条目未处理")
    return {
        "total": total,
        "by_state": counts,
        "pending": pending,
        "doctor_reviewed": doctor_reviewed,
        "auto_processed": auto_processed,
        "tier_counts": tier_counts,
        # 平铺字段方便队列摘要和旧版前端读取。
        "required": tier_counts[REQUIRED],
        "sample": tier_counts[SAMPLE],
        "automatic": tier_counts[AUTOMATIC],
        "sample_pending": sample_pending,
        "high_risk": high_risk,
        "conflict": conflict,
        "analysis_failed": analysis_failed,
        "specialty_total": len(specialty_items),
        "specialty_pending": specialty_pending,
        "specialty_reviewed": specialty_reviewed,
        "specialty_failed": specialty_failed,
        "batch_blocked": batch_blocked,
        "batch_blocked_pending": batch_blocked_pending,
        # 兼容字段：语义收紧为"医生真正处理过"（此前把免审放行也算进去，分不清人和规则）
        "human_touched": doctor_reviewed,
        "all_resolved": total > 0 and pending == 0 and sample_pending == 0,
        "can_publish": total > 0 and pending == 0,
        "blocking_reasons": blocking,
        "progress": round((total - pending - sample_pending) / total, 2) if total else 0.0,
    }


def make_decision(*, case_id: str, item: ReviewItem, action: str, after: str = "",
                  reason: str = "", reviewer_name: str = "模拟审核员",
                  reviewer_dept: str = "", version: int = 1,
                  content_revision: Optional[int] = None,
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
        reviewer=Reviewer(role=reviewer_name or "模拟审核员", dept=reviewer_dept),
        evidence_refs=list(item.evidence_refs),
        version=int(version),
        content_revision=int(content_revision or item.content_revision or 1),
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
