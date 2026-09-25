"""报告体系：患者版一页摘要 + 医生审核版完整报告 + Markdown 渲染。

对应《产品开发规格书-比赛版v-competition.md》任务 3.5 与主文档 §12。

红线（写死）：
- 患者版**禁止**把未审核推断写成确定诊断或治疗意见；
- 任何未经医生确认的输出必须带 **"AI 生成，尚未经医生审核"** 角标（规格书 §3.3）；
- 报告必填段落完整率 100%（规格书 §10 验收线）；
- 医疗行为一律"建议向医生确认是否需要…"，不得下指令。
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.evidence_service import (  # noqa: E402
    STATE_LABELS,
    build_evidence_cards,
    build_timeline,
    summarize_evidence,
)
from services.review_service import (  # noqa: E402
    ACTION_LABELS as REVIEW_ACTION_LABELS,
    TARGET_TYPE_LABELS,
)
from services.department_domain import ALL_NAMES  # noqa: E402

# 未审核角标 / 免责（固定文案，所有导出件必带）
AI_BADGE = "AI 生成，尚未经医生审核"
DISCLAIMER = (
    "本报告由 AI 辅助生成，仅用于就医信息整理与就诊参考，"
    "不构成诊断、处方或治疗意见；所有结论以线下医生判断为准。"
)
CAPABILITY_BOUNDARY = (
    "本系统不能做什么：不替代医生诊断与处方；不处理急诊抢救、儿科急症与精神危机；"
    "不给用药剂量建议；不承诺排查结果完整。如有急症表现请立即就医。"
)

# 完整报告必填段（规格书 §10：必填结构字段完整率 100%）
REQUIRED_SECTIONS = (
    "病例画像", "时间线", "分诊结果", "就医路径", "各专科 AI 视角",
    "共识", "分歧", "不确定性", "关键待解决问题", "证据关联",
    "医生最终意见", "AI 与医生意见差异", "审核记录与版本",
)

# ---------------------------------------------------------------------------
# 报告展示层：把内部数据结构与代码值翻成"人看得懂的中文"
# ---------------------------------------------------------------------------
# 设计约束（对外呈现）：
# - 报告里**不得**出现内部字段名（judgement / evidence_state / supports …）或代码值
#   （direct / rule-dominant / awaiting_doctor …），也不得出现 `[object Object]` 式的残渣；
# - **不得**输出模型自报置信度（规格书 §4.3 红线）——原始数据里的 confidence 一律丢弃；
# - 结构保持"中文键 → 字符串 / 字符串数组 / 卡片对象数组"，供前端逐条渲染。

TIMELINE_KIND_LABELS: Dict[str, str] = {
    "symptom": "症状", "diagnosis": "既往疾病", "exam": "检查结果", "medication": "当前用药",
}
STEP_KIND_LABELS: Dict[str, str] = {
    "prepare": "资料准备", "visit": "就诊", "check": "检查", "followup": "复诊",
}
SCOPE_LABELS: Dict[str, str] = {
    "triage": "就医方向判断", "specialty": "专科分析判断", "disagreement": "分歧条目",
}
LEVEL_LABELS: Dict[str, str] = {"direct": "直接引证", "indirect": "间接支持"}
SOURCE_KIND_LABELS: Dict[str, str] = {"check": "检查类建议", "gap": "信息缺口"}
SOURCE_TYPE_LABELS: Dict[str, str] = {
    "ai_reference": "专科 AI 参考意见",
    "human_reviewed": "医生审核内容",
}
DECISION_PATH_LABELS: Dict[str, str] = {
    "rule-dominant": "以规则为主（模型仅作补充）",
    "model-dominant": "以模型为主（规则未命中强项）",
    "mixed": "规则与模型共同决定",
    "refused": "已拒绝作答（触发安全约束）",
    "degraded": "降级运行（模型未生效，仅规则链路）",
}
CASE_STATE_LABELS: Dict[str, str] = {
    "pending_materials": "资料待确认", "analyzing": "AI 分析中",
    "awaiting_doctor": "AI 分析完成 / 待医生审核", "need_more_info": "退回补资料",
    "doctor_reviewed": "医生已审阅", "doctor_confirmed": "医生已确认",
    "awaiting_recheck": "新资料进入后待复核",
}
BEHAVIOR_LABELS: Dict[str, str] = {
    "A": "A 型（单科为主）", "B": "B 型（多病共病）", "C": "C 型（诊断未明）",
    "D": "D 型（围术期）", "E": "E 型（其他）",
}


def _refs_text(refs: Any) -> str:
    refs = list(refs or [])
    return "、".join(str(r) for r in refs) if refs else "（无原文出处）"


def _yes_no(value: Any) -> str:
    return "是" if value else "否"


def _decision_map(decisions: Any) -> Dict[str, Dict[str, Any]]:
    """当前审核轮次内，按条目保留最后一次医生决定。"""
    out: Dict[str, Dict[str, Any]] = {}
    for decision in decisions or []:
        key = f"{decision.get('target_type')}:{decision.get('target_id')}"
        out[key] = decision
    return out


def _department_from_review(text: Any) -> str:
    """从医生的整句改写中提取域内科室名，兼容“牵头建议：眼科”这类内容。"""
    value = str(text or "").strip()
    matches = [name for name in ALL_NAMES if name in value]
    return max(matches, key=len) if matches else value


def _reviewed_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """生成报告专用有效视图：修改覆盖、驳回移除，不篡改病例原始审计数据。"""
    reviewed = copy.deepcopy(case or {})
    payload = reviewed.get("payload") or {}
    decisions = _decision_map(payload.get("decisions") or [])

    triage = payload.get("triage") or {}
    picks = []
    if triage.get("primary"):
        picks.append(("primary", triage["primary"]))
    picks.extend(("secondary", item) for item in (triage.get("secondary") or []))
    kept = []
    for original_group, pick in picks:
        dep = str(pick.get("department") or "")
        decision = decisions.get(f"department_selection:{dep}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(pick)
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["department"] = _department_from_review(decision["after"])
            updated["reason"] = "医生审核后修改"
        kept.append((original_group, updated))
    # 医生把牵头方向改为原有协同科室时只保留一份，避免报告出现重复科室。
    unique_kept = []
    seen_departments = set()
    for group, item in kept:
        department = str(item.get("department") or "")
        if department and department in seen_departments:
            continue
        seen_departments.add(department)
        unique_kept.append((group, item))
    kept = unique_kept
    primary = next((item for group, item in kept if group == "primary"), None)
    secondary = [item for group, item in kept if group == "secondary"]
    if primary is None and secondary:
        primary = secondary.pop(0)
        primary["role"] = "牵头"
    triage["primary"] = primary
    triage["secondary"] = secondary
    triage["selected"] = ([primary.get("department")] if primary else []) + [
        item.get("department") for item in secondary if item.get("department")]

    path = payload.get("path") or {}
    effective_steps = []
    for step in path.get("steps") or []:
        decision = decisions.get(f"path_step:{step.get('step_id')}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(step)
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["title"] = str(decision["after"]).strip()
            updated["detail"] = "医生审核后修改"
        effective_steps.append(updated)
    path["steps"] = effective_steps

    aggregation = (payload.get("mdt") or {}).get("aggregation") or {}
    effective_consensus = []
    for index, item in enumerate(aggregation.get("consensus") or [], start=1):
        decision = decisions.get(f"consensus_item:C-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(item)
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["example"] = str(decision["after"]).strip()
        effective_consensus.append(updated)
    aggregation["consensus"] = effective_consensus

    effective_disagreements = []
    for index, item in enumerate(aggregation.get("disagreements") or [], start=1):
        decision = decisions.get(f"consensus_item:X-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(item)
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["reason"] = str(decision["after"]).strip()
        effective_disagreements.append(updated)
    aggregation["disagreements"] = effective_disagreements
    return reviewed


def _apply_advice_decisions(values: List[str], section: str,
                            decisions: Dict[str, Dict[str, Any]]) -> List[str]:
    effective: List[str] = []
    for index, value in enumerate(values, start=1):
        decision = decisions.get(f"patient_advice:{section}-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            effective.append(str(decision["after"]).strip())
        else:
            effective.append(value)
    return effective


def timeline_for_report(events: Any) -> List[Dict[str, Any]]:
    """时间线 → 中文键事件列表（丢弃模型自报置信度）。"""
    out: List[Dict[str, Any]] = []
    for event in events or []:
        row: Dict[str, Any] = {
            "时间": event.get("when") or "时间不详",
            "类型": TIMELINE_KIND_LABELS.get(str(event.get("kind")), "事件"),
            "内容": event.get("title") or "",
        }
        if event.get("detail"):
            row["说明"] = event["detail"]
        row["原文出处"] = _refs_text(event.get("source_refs"))
        out.append(row)
    return out


def path_steps_for_report(steps: Any) -> List[Dict[str, Any]]:
    """就医路径步骤 → 中文键列表。"""
    out: List[Dict[str, Any]] = []
    for index, step in enumerate(steps or [], start=1):
        row: Dict[str, Any] = {
            "步骤": step.get("step_id") or f"S{index}",
            "类型": STEP_KIND_LABELS.get(str(step.get("kind")), "步骤"),
            "内容": step.get("title") or "",
        }
        if step.get("detail"):
            row["说明"] = step["detail"]
        if step.get("condition"):
            row["执行条件"] = step["condition"]
        if step.get("depends_on"):
            row["依赖步骤"] = "、".join(str(x) for x in step["depends_on"])
        notes = []
        if step.get("parallel"):
            notes.append("可与前一步并行")
        if step.get("requires_doctor"):
            notes.append("需医生决定")
        if notes:
            row["备注"] = "；".join(notes)
        out.append(row)
    return out


def evidence_cards_for_report(cards: Any) -> List[Dict[str, Any]]:
    """证据卡片 → 中文键卡片（支持 / 反向不足 / 外部依据 三块，缺就写明缺）。"""
    out: List[Dict[str, Any]] = []
    for card in cards or []:
        row: Dict[str, Any] = {
            "判断": card.get("judgement") or "",
            "判断类型": SCOPE_LABELS.get(str(card.get("scope")), "判断"),
        }
        if card.get("department"):
            row["科室"] = card["department"]
        if card.get("reason"):
            row["依据"] = card["reason"]
        row["证据状态"] = STATE_LABELS.get(str(card.get("evidence_state")),
                                       str(card.get("evidence_state") or "—"))
        row["证据充分度"] = {
            "sufficient": "充分", "partial": "部分充分", "insufficient": "不充分",
        }.get(str(card.get("evidence_sufficiency")), "未评估")
        row["信息一致性"] = {
            "consistent": "一致", "conflict": "存在冲突", "unable_to_compare": "无法比较",
        }.get(str(card.get("information_consistency")), "未评估")
        row["审核状态"] = {
            "ai_unreviewed": "AI 生成，未审核", "doctor_reviewed": "已审阅",
            "doctor_confirmed": "已确认", "need_more_info": "需补充资料",
        }.get(str(card.get("review_status")), "未审核")
        supports = [{"证据": s.get("text") or "",
                     "原文出处": _refs_text(s.get("source_refs")),
                     "强度": LEVEL_LABELS.get(str(s.get("level")), "支持")}
                    for s in (card.get("supports") or [])]
        row["支持证据"] = supports or ["（暂无支持证据：该判断的依据不足）"]
        related = [str(item.get("text") or "") for item in (card.get("related_candidates") or [])]
        if related:
            row["相关资料候选（未验证支持关系）"] = related
        opposes = [{"证据": o.get("text") or "", "说明": o.get("reason") or ""}
                   for o in (card.get("opposes") or [])]
        row["反向 / 不足证据"] = opposes or ["（未发现反向证据）"]
        external = [{"依据": e.get("title") or "", "来源": e.get("source") or "",
                     "要点": e.get("note") or "", "版本": e.get("version") or "未补齐",
                     "标识符": e.get("identifier") or "未补齐",
                     "官方链接": e.get("url") or "未补齐",
                     "最近复核": e.get("reviewed_at") or "未复核",
                     "可追溯": "是" if e.get("traceable") else "否（仅作参考候选）"}
                    for e in (card.get("external") or [])]
        row["外部依据"] = external or ["（暂无可靠外部依据，本版不做文献检索）"]
        if card.get("resolving_info"):
            row["如何收敛"] = card["resolving_info"]
        out.append(row)
    return out


def specialty_views_for_report(analyses: Any) -> List[Dict[str, Any]]:
    """各专科 AI 视角 → 中文键卡片（**不含模型自报置信度**）。"""
    out: List[Dict[str, Any]] = []
    for analysis in analyses or []:
        dept = analysis.get("department") or "（未知科室）"
        if analysis.get("status") != "ok":
            out.append({"科室": dept, "来源": SOURCE_TYPE_LABELS["ai_reference"],
                        "状态": "本版未生成（模型未配置或调用失败）"})
            continue
        row: Dict[str, Any] = {"科室": dept, "来源": SOURCE_TYPE_LABELS["ai_reference"]}
        if analysis.get("focus"):
            row["关注点"] = analysis["focus"]
        row["客观事实"] = list(analysis.get("facts") or []) or ["（无）"]
        row["分析判断"] = [str(i.get("text") or "") for i in (analysis.get("inferences") or [])] or ["（无）"]
        row["待确认事项"] = [str(k.get("content") or "") for k in (analysis.get("checks") or [])] or ["（无）"]
        row["该科信息缺口"] = list(analysis.get("information_gaps") or []) or ["（无）"]
        if analysis.get("referral"):
            row["建议转诊协作"] = analysis["referral"]
        out.append(row)
    return out


def build_mdt_preparation_packet(case: Dict[str, Any]) -> Dict[str, Any]:
    """从现有结构化病例即时生成医生可读的 MDT 会前资料包。

    资料包只组合已经脱敏和结构化的数据，不复制完整病史；它是审核接口的
    派生视图，不落库。专科分析由系统生成时固定标记为 ``ai_reference``，
    只有已验证的医生端线下意见才进入 ``human_reviewed`` 区域。
    """
    case = case or {}
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    triage = payload.get("triage") or {}
    path = payload.get("path") or {}
    mdt = payload.get("mdt") or {}
    aggregation = mdt.get("aggregation") or {}
    screening_raw = payload.get("mdt_screening") or {}

    screening = {
        "status": screening_raw.get("status") or "pending",
        "decision": screening_raw.get("decision") or "",
        "decision_label": screening_raw.get("decision_label") or "待医生初筛",
        "reason": screening_raw.get("reason") or "",
        "purpose": screening_raw.get("purpose") or "",
        "departments": list(screening_raw.get("departments") or []),
        "mdt_queue_eligible": bool(screening_raw.get("mdt_queue_eligible")),
    }

    timeline_raw = build_timeline(profile)
    timeline = timeline_for_report(timeline_raw)
    evidence_cards = build_evidence_cards(profile, triage, mdt)
    evidence = evidence_cards_for_report(evidence_cards)
    evidence_refs = sorted({
        str(ref)
        for event in timeline_raw
        for ref in (event.get("source_refs") or [])
        if str(ref).strip()
    })

    specialty_opinions: List[Dict[str, Any]] = []
    unavailable_departments: List[str] = []
    for analysis in mdt.get("analyses") or []:
        department = str(analysis.get("department") or "（未知科室）")
        source_type = "ai_reference"
        if analysis.get("status") != "ok":
            unavailable_departments.append(department)
            specialty_opinions.append({
                "department": department,
                "status": "unavailable",
                "source_type": source_type,
                "source_label": SOURCE_TYPE_LABELS[source_type],
                "notice": "本专科参考意见暂未生成，不能按已完成意见使用。",
            })
            continue
        specialty_opinions.append({
            "department": department,
            "status": "available",
            "source_type": source_type,
            "source_label": SOURCE_TYPE_LABELS[source_type],
            "focus": str(analysis.get("focus") or ""),
            "facts": [str(item) for item in (analysis.get("facts") or [])[:6]],
            "inferences": [
                {
                    "text": str(item.get("text") or ""),
                    "evidence_refs": [str(ref) for ref in (item.get("evidence_refs") or [])],
                }
                for item in (analysis.get("inferences") or [])[:4]
            ],
            "checks": [
                {
                    "priority": str(item.get("priority") or "medium"),
                    "content": str(item.get("content") or ""),
                }
                for item in (analysis.get("checks") or [])[:5]
            ],
            "information_gaps": [
                str(item) for item in (analysis.get("information_gaps") or [])[:6]
            ],
            "referral": str(analysis.get("referral") or ""),
        })

    human_reviewed: List[Dict[str, Any]] = []
    unverified_opinion_count = 0
    for opinion in payload.get("doctor_opinions") or []:
        if not opinion.get("is_verified"):
            unverified_opinion_count += 1
            continue
        human_reviewed.append({
            "department": str(opinion.get("department") or "线下医生"),
            "content": str(opinion.get("opinion") or ""),
            "at": str(opinion.get("at") or ""),
            "source_type": "human_reviewed",
            "source_label": SOURCE_TYPE_LABELS["human_reviewed"],
        })

    consensus = consensus_for_report(aggregation.get("consensus"))
    disagreements = disagreements_for_report(aggregation.get("disagreements"))
    uncertainties = uncertainties_for_report(aggregation.get("uncertainties"))
    handling = {
        "disagreements": [
            {
                "topic": item.get("分歧主题") or "",
                "status": "待医生裁决",
                "next_step": item.get("如何收敛") or "需补充更明确的证据后再判断",
            }
            for item in disagreements
        ],
        "information_gaps": [
            {"item": str(item), "status": "待补充资料"}
            for item in (payload.get("gaps") or [])[:10]
        ],
        "unavailable_specialties": [
            {"department": department, "status": "待补齐意见"}
            for department in unavailable_departments
        ],
    }

    primary = triage.get("primary") or {}
    packet = {
        "case_id": str(case.get("case_id") or ""),
        "screening": screening,
        "patient_summary": {
            "chief_complaint": str(profile.get("chief_complaint") or case.get("main_issue") or ""),
            "age": profile.get("age"),
            "sex": profile.get("sex") or "",
        },
        "triage": {
            "primary_department": str(primary.get("department") or ""),
            "secondary_departments": [
                str(item.get("department") or "")
                for item in (triage.get("secondary") or [])
                if item.get("department")
            ],
            "reliability": str((triage.get("reliability") or {}).get("level") or "—"),
            "reliability_reasons": [
                str(item) for item in ((triage.get("reliability") or {}).get("reasons") or [])[:4]
            ],
        },
        "timeline": timeline,
        "evidence_links": [
            {"ref": ref, "source": "患者结构化资料"} for ref in evidence_refs
        ],
        "evidence": evidence,
        "information_gaps": [str(item) for item in (payload.get("gaps") or [])[:10]],
        "path": path_steps_for_report(path.get("steps")),
        "tasks": [
            {
                "type": str(item.get("type") or ""),
                "content": str(item.get("content") or ""),
                "priority": str(item.get("priority") or ""),
                "state": str(item.get("state") or ""),
            }
            for item in (payload.get("tasks") or path.get("tasks") or [])[:10]
        ],
        "specialty_opinions": specialty_opinions,
        "human_reviewed": human_reviewed,
        "unverified_opinion_count": unverified_opinion_count,
        "consensus": consensus,
        "disagreements": disagreements,
        "uncertainties": uncertainties,
        "open_questions": [str(item) for item in (aggregation.get("open_questions") or [])[:10]],
        "handling": handling,
        "notes": [
            "以上专科内容均为 AI 参考意见，不是真实专科医生意见。",
            "分歧、缺失证据和未生成的科室意见需由医生结合线下资料处理。",
        ],
        "status": {
            "mdt_available": bool(specialty_opinions and not unavailable_departments),
            "unavailable_departments": unavailable_departments,
        },
        "source_legend": dict(SOURCE_TYPE_LABELS),
    }
    return packet


def consensus_for_report(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        row: Dict[str, Any] = {
            "共识主题": item.get("topic") or "",
            "支持科室": "、".join(str(x) for x in (item.get("supporters") or [])) or "（无）",
        }
        sources = [SOURCE_KIND_LABELS.get(str(s), str(s)) for s in (item.get("sources") or [])]
        if sources:
            row["来源类型"] = "、".join(sources)
        if item.get("example"):
            row["原始表述"] = item["example"]
        out.append(row)
    return out


def disagreements_for_report(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        positions = item.get("positions") or {}
        out.append({
            "分歧主题": item.get("topic") or "",
            "各方立场": "；".join(f"{k}：{v}" for k, v in positions.items()) or "（未记录）",
            "分歧原因": item.get("reason") or "（未记录）",
            "如何收敛": item.get("resolving_info") or "（未记录）",
            "证据状态": STATE_LABELS.get(str(item.get("evidence_state")), "—"),
        })
    return out


def uncertainties_for_report(items: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in items or []:
        out.append({
            "不确定事项": item.get("topic") or "",
            "影响范围": "、".join(str(x) for x in (item.get("impact") or [])) or "（未标注）",
        })
    return out


def recent_decisions_for_report(decisions: Any, limit: int = 3) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for decision in list(decisions or [])[-limit:]:
        out.append({
            "时间": decision.get("created_at") or "",
            "动作": REVIEW_ACTION_LABELS.get(str(decision.get("action")), str(decision.get("action"))),
            "条目": TARGET_TYPE_LABELS.get(str(decision.get("target_type")),
                                          str(decision.get("target_type"))),
            "原因": decision.get("reason") or "（未填写）",
        })
    return out


@dataclass
class Report:
    """一份报告（患者版或完整版）。"""

    kind: str = "full"                       # summary（一页摘要） / full（完整报告）
    case_id: str = ""
    generated_at: str = ""
    badge: str = AI_BADGE
    disclaimer: str = DISCLAIMER
    sections: Dict[str, Any] = field(default_factory=dict)
    missing_sections: List[str] = field(default_factory=list)
    doctor_confirmed: bool = False
    stage: str = "ai_preparation"
    report_version: int = 1
    review_status: str = "ai_unreviewed"

    @property
    def stage_label(self) -> str:
        if self.stage == "doctor_reviewed":
            return "医生审核版报告" if self.kind == "full" else "医生审核版摘要"
        return "AI 就诊准备报告" if self.kind == "full" else "AI 就诊准备摘要"

    @property
    def review_status_label(self) -> str:
        return "已发布医生审核版" if self.review_status == "doctor_confirmed" else "AI 生成，尚未经医生审核"

    @property
    def complete(self) -> bool:
        """必填段是否齐全（完整报告口径）。"""
        return not self.missing_sections

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "case_id": self.case_id,
            "generated_at": self.generated_at,
            "badge": ("" if self.doctor_confirmed else self.badge),
            "doctor_confirmed": self.doctor_confirmed,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "review_status": self.review_status,
            "review_status_label": self.review_status_label,
            "report_version": self.report_version,
            "sections": self.sections,
            # JSON 序列化（Flask jsonify）会按 key 排序，因此额外给出**权威段落顺序**
            # （完整报告须遵循规格书 §12.2 的 1-13 段，不能被打乱）
            "section_order": list(self.sections.keys()),
            "missing_sections": list(self.missing_sections),
            "complete": self.complete,
            "disclaimer": self.disclaimer,
            "capability_boundary": CAPABILITY_BOUNDARY,
        }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 患者版一页摘要（主文档 §12.1）
# ---------------------------------------------------------------------------

def build_patient_summary(case: Dict[str, Any]) -> Report:
    case = _reviewed_case(case)
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    triage = payload.get("triage") or {}
    path = payload.get("path") or {}
    tasks = payload.get("tasks") or []

    primary = triage.get("primary") or {}
    secondary = triage.get("secondary") or []
    gaps = [g for g in (payload.get("gaps") or [])]
    decisions = _decision_map(payload.get("decisions") or [])
    confirmed = case.get("case_state") == "doctor_confirmed"

    consult_questions: List[str] = []
    for t in tasks:
        if t.get("type") == "就医咨询":
            content = str(t.get("content") or "").strip()
            if content and content not in consult_questions:
                consult_questions.append(content)
    for index, q in enumerate(
            ((payload.get("mdt") or {}).get("aggregation") or {}).get("open_questions") or [],
            start=1):
        decision = decisions.get(f"patient_advice:Q-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        text = str(decision.get("after") or q).strip() \
            if decision.get("action") == "modify" else str(q).strip()
        if text and text not in consult_questions:
            consult_questions.append(text)
        if len(consult_questions) >= 6:
            break
    consult_questions = consult_questions[:6]
    consult_questions = _apply_advice_decisions(
        consult_questions, "需要向医生确认的问题", decisions)

    report = Report(kind="summary", case_id=case.get("case_id", ""),
                    generated_at=_now(),
                    doctor_confirmed=confirmed,
                    stage="doctor_reviewed" if confirmed else "ai_preparation",
                    report_version=int(case.get("report_version") or 1),
                    review_status="doctor_confirmed" if confirmed else "ai_unreviewed")
    direction_values = _apply_advice_decisions(
        [_departments_line(primary, secondary)], "建议咨询方向", decisions)
    report.sections = {
        "主要问题": profile.get("chief_complaint") or case.get("main_issue") or "（未提供）",
        "关键已知资料": _known_facts(profile),
        "当前信息缺口": gaps or ["（暂无）"],
        "建议咨询方向": direction_values[0] if direction_values else "（已由医生驳回）",
        "需要向医生确认的问题": consult_questions or ["（暂无）"],
        "当前任务": [f"{t.get('type')}｜{t.get('content')}" for t in tasks[:6]] or ["（暂无）"],
        "结果可靠性": (triage.get("reliability") or {}).get("level", "—"),
        "可靠性原因": (triage.get("reliability") or {}).get("reasons") or [],
    }
    return report


def _known_facts(profile: Dict[str, Any]) -> List[str]:
    facts: List[str] = []
    for item in (profile.get("diseases") or [])[:4]:
        facts.append(f"{item.get('name')}（{item.get('duration') or '病程不详'}）")
    for item in (profile.get("exams") or [])[:4]:
        facts.append(f"{item.get('item')} {item.get('value') or ''}".strip())
    for item in (profile.get("medications") or [])[:3]:
        facts.append(f"用药：{item.get('name')} {item.get('dose') or ''}".strip())
    return facts


def _departments_line(primary: Dict[str, Any], secondary: List[Dict[str, Any]]) -> str:
    if not primary and not secondary:
        return "（暂未给出明确方向，请补充资料或咨询医生）"
    parts = []
    if primary:
        parts.append(f"{primary.get('department')}（牵头方向）")
    for d in secondary:
        parts.append(f"{d.get('department')}（协同）")
    return "、".join(parts)


# ---------------------------------------------------------------------------
# 医生审核版完整报告（主文档 §12.2，13 段）
# ---------------------------------------------------------------------------

def build_full_report(case: Dict[str, Any]) -> Report:
    case = _reviewed_case(case)
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    triage = payload.get("triage") or {}
    path = payload.get("path") or {}
    mdt = payload.get("mdt") or {}
    aggregation = mdt.get("aggregation") or {}
    decisions = payload.get("decisions") or []
    decision_lookup = _decision_map(decisions)

    cards = build_evidence_cards(profile, triage, mdt)
    review_status = {
        "doctor_confirmed": "doctor_confirmed",
        "doctor_reviewed": "doctor_reviewed",
        "need_more_info": "need_more_info",
    }.get(str(case.get("case_state") or ""), "ai_unreviewed")
    for card in cards:
        card["review_status"] = review_status
        card["doctor_confirmed"] = review_status == "doctor_confirmed"
    evidence_stat = summarize_evidence(cards)

    confirmed = case.get("case_state") == "doctor_confirmed"
    report = Report(kind="full", case_id=case.get("case_id", ""),
                    generated_at=_now(),
                    doctor_confirmed=confirmed,
                    stage="doctor_reviewed" if confirmed else "ai_preparation",
                    report_version=int(case.get("report_version") or 1),
                    review_status="doctor_confirmed" if confirmed else "ai_unreviewed")

    reliability = triage.get("reliability") or {}
    open_questions: List[str] = []
    for index, question in enumerate(aggregation.get("open_questions") or [], start=1):
        decision = decision_lookup.get(f"patient_advice:Q-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        open_questions.append(
            str(decision.get("after") or question) if decision.get("action") == "modify"
            else str(question))

    sections: Dict[str, Any] = {
        "病例画像": {
            "基本信息": f"{profile.get('sex') or ''} {profile.get('age') or ''}".strip() or "（未提供）",
            "主诉": profile.get("chief_complaint") or "（未提供）",
            "既往疾病": [f"{d.get('name')} {d.get('duration') or ''}".strip()
                       for d in (profile.get("diseases") or [])] or ["（未提供）"],
            "检查异常": [f"{e.get('item')} {e.get('value') or ''}".strip()
                       for e in (profile.get("exams") or [])] or ["（未提供）"],
            "当前用药": [f"{m.get('name')} {m.get('dose') or ''}".strip()
                       for m in (profile.get("medications") or [])] or ["（未提供）"],
            "信息缺口": list(payload.get("gaps") or []) or ["（无）"],
            "资料完整度": payload.get("completeness"),
            "原文直引覆盖率": payload.get("direct_quote_coverage"),
        },
        "时间线": timeline_for_report(build_timeline(profile)),
        "分诊结果": {
            "病例类型": BEHAVIOR_LABELS.get(str(triage.get("behavior_type")), "（未判定）"),
            "牵头方向": (triage.get("primary") or {}).get("department") or "（未给出）",
            "协同方向": [d.get("department") for d in (triage.get("secondary") or [])] or ["（无）"],
            "决策路径": DECISION_PATH_LABELS.get(str(triage.get("decision_path")), "（未记录）"),
            "结果可靠性": reliability.get("level") or "（未评估）",
            "可靠性说明": list(reliability.get("reasons") or []) or ["（无）"],
            "是否需医生复核": _yes_no(triage.get("needs_doctor_review")),
            "规则校验说明": list(triage.get("validator_notes") or []) or ["（无）"],
        },
        "就医路径": path_steps_for_report(path.get("steps")),
        "各专科 AI 视角": specialty_views_for_report(mdt.get("analyses")),
        "共识": consensus_for_report(aggregation.get("consensus")),
        "分歧": disagreements_for_report(aggregation.get("disagreements")),
        "不确定性": uncertainties_for_report(aggregation.get("uncertainties")),
        "关键待解决问题": open_questions,
        "证据关联": {
            "证据状态统计": [
                (f"{STATE_LABELS[k]}：{v} 条" if k in STATE_LABELS else f"卡片合计：{v} 张")
                for k, v in sorted(evidence_stat.items(), key=lambda kv: kv[0] not in STATE_LABELS)
            ],
            "状态说明": [f"{label}：{desc}" for label, desc in {
                "有明确证据": "有可溯源的原文或检查直接支持",
                "证据间接支持": "仅有间接依据或规则信号",
                "缺少证据": "当前资料不足以支持该判断",
                "与现有证据冲突": "存在相反倾向的判断或缺失项",
                "已被医生确认": "医生已在审核台确认",
                "等待医生确认": "尚未审核，需医生判断",
            }.items()],
            "证据卡片": evidence_cards_for_report(cards),
        },
        "医生最终意见": _doctor_opinions(decisions),
        "AI 与医生意见差异": _differences(decisions),
        "审核记录与版本": {
            "报告版本": case.get("report_version", 1),
            "病例状态": CASE_STATE_LABELS.get(str(case.get("case_state")), "（未记录）"),
            "审核条目数": len(decisions),
            # 免审条目如实单列：规则判定免审 ≠ 医生确认（红线），不能让读者以为医生看过
            "免审条目": _exempt_note(payload),
            "最近操作": recent_decisions_for_report(decisions),
        },
    }

    report.sections = sections
    # 必填段完整率 100%：未能生成的段落给出**说明性占位**（诚实标注未生成本版），
    # 而不是留空 —— 否则"结构完整性"验收线无法达成。
    if not sections.get("各专科 AI 视角"):
        # 让占位文案说清"为什么没有多学科这一段"：单科 / A 型本来就不该硬凑 MDT
        triage = payload.get("triage") or {}
        if len(triage.get("selected") or []) < 2:
            sections["各专科 AI 视角"] = "（仅 1 个就医方向，本版未运行多学科会诊）"
        elif triage and not triage.get("needs_mdt"):
            sections["各专科 AI 视角"] = "（本例以单科为主，未运行多学科会诊）"
    report.missing_sections = []
    for name in REQUIRED_SECTIONS:
        value = sections.get(name)
        if value in (None, [], {}):
            sections[name] = "（本版本未生成：模型未配置或无可用意见卡/审核记录）"
    return report


def _exempt_note(payload: Dict[str, Any]) -> str:
    """免审条目的报告口径：**不计入医生审核**（红线：规则放行 ≠ 医生确认）。"""
    review = (payload or {}).get("review") or {}
    auto = int(review.get("auto_processed") or 0)
    if not auto:
        return "0 条"
    return f"{auto} 条（路径步骤未要求医生决定，未计入医生审核）"


def _doctor_opinions(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"条目": TARGET_TYPE_LABELS.get(str(d.get("target_type")), str(d.get("target_type"))),
             "操作": REVIEW_ACTION_LABELS.get(str(d.get("action")), str(d.get("action"))),
             "内容": d.get("after") or d.get("before"),
             "原因": d.get("reason") or "（未填写）",
             "审核人": (d.get("reviewer") or {}).get("role") or "（未记录）",
             "时间": d.get("created_at") or ""}
            for d in decisions if d.get("action") in ("accept", "modify", "supplement_opinion")]


def _differences(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """AI 建议 vs 医生意见的差异（医生修改/驳回即差异）。"""
    out = []
    for d in decisions:
        if d.get("action") in ("modify", "reject"):
            out.append({
                "条目": TARGET_TYPE_LABELS.get(str(d.get("target_type")), str(d.get("target_type"))),
                "AI 原意见": d.get("before"),
                "医生意见": d.get("after") or "（已驳回，不进入报告）",
                "差异类型": "医生修改" if d.get("action") == "modify" else "医生驳回",
                "原因": d.get("reason") or "（未填写）",
            })
    return out


# ---------------------------------------------------------------------------
# Markdown 渲染（浏览器"打印为 PDF"用）
# ---------------------------------------------------------------------------

def render_markdown(report: Report) -> str:
    lines: List[str] = []
    title = report.stage_label
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 病例号：{report.case_id}　生成时间：{report.generated_at}")
    lines.append(f"> 阶段：{report.stage_label}　报告版本：{report.report_version}")
    if not report.doctor_confirmed:
        lines.append(f"> ⚠️ **{report.review_status_label}**")
    else:
        lines.append("> ✅ 已发布医生审核版")
    lines.append("")
    lines.append("---")
    lines.append("")

    for name, content in report.sections.items():
        lines.append(f"## {name}")
        lines.append("")
        lines.extend(_render_value(content))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"> {report.disclaimer}")
    lines.append(">")
    lines.append(f"> {CAPABILITY_BOUNDARY}")
    return "\n".join(lines)


def _render_value(value: Any, indent: int = 0) -> List[str]:
    pad = "  " * indent
    out: List[str] = []
    if value is None or value == "":
        return [f"{pad}（无）"]
    if isinstance(value, str):
        return [f"{pad}{value}"]
    if isinstance(value, (int, float, bool)):
        return [f"{pad}{value}"]
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)):
                out.append(f"{pad}- **{k}**：")
                out.extend(_render_value(v, indent + 1))
            else:
                out.append(f"{pad}- **{k}**：{v if v not in (None, '') else '（无）'}")
        return out
    if isinstance(value, list):
        if not value:
            return [f"{pad}（无）"]
        for item in value:
            if isinstance(item, dict):
                # 单行摘要式展示
                summary = "；".join(f"{k}={v}" for k, v in item.items()
                                    if v not in (None, "", [], {}))
                out.append(f"{pad}- {summary}")
            else:
                out.append(f"{pad}- {item}")
        return out
    return [f"{pad}{value}"]


def build_both(case: Dict[str, Any]) -> Dict[str, Any]:
    """同时生成患者版与完整版（API 一次性返回）。"""
    summary = build_patient_summary(case)
    full = build_full_report(case)
    return {
        "summary": summary.to_dict(),
        "full": full.to_dict(),
        "summary_markdown": render_markdown(summary),
        "full_markdown": render_markdown(full),
    }
