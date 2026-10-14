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
    DISAGREEMENT_STATUS_LABELS,
    TARGET_TYPE_LABELS,
    latest_disagreement_decisions,
    build_items, current_decisions, specialty_analysis_summary,
)

from services.doctor_mdt import review_analysis
from services.dependency_matrix import case_relevant_gaps  # noqa: E402

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


def _reviewed_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """生成报告专用有效视图：修改覆盖、驳回移除，不篡改病例原始审计数据。"""
    reviewed = copy.deepcopy(case or {})
    payload = reviewed.get("payload") or {}
    effective_decisions = current_decisions(build_items(case), payload.get("decisions") or [])
    decisions = _decision_map(effective_decisions)
    # 报告视图只应用当前内容修订内的决定；原始 decisions 继续保留，供审计历史展示。
    payload["_effective_decisions"] = copy.deepcopy(effective_decisions)

    triage = payload.get("triage") or {}
    confirmation = payload.get("triage_doctor_confirmation") or {}
    if confirmation.get("status") == "confirmed":
        primary_department = str(confirmation.get("primary_department") or "").strip()
        secondary_departments = [str(dep).strip() for dep in (confirmation.get("secondary_departments") or []) if str(dep).strip()]
        triage["primary"] = {"department": primary_department, "role": "牵头", "reason": "医生分流确认"} if primary_department else None
        triage["secondary"] = [
            {"department": dep, "role": "协同", "reason": "医生分流确认"}
            for dep in secondary_departments if dep != primary_department
        ]
        triage["selected"] = ([primary_department] if primary_department else []) + [
            dep for dep in secondary_departments if dep != primary_department
        ]

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

    payload["mdt"] = copy.deepcopy(review_analysis(payload))
    aggregation = (payload.get("mdt") or {}).get("aggregation") or {}
    # 同一分歧复制到患者问题清单时，沿用医生对该分歧的处理。
    effective_questions = []
    for value in aggregation.get("open_questions") or []:
        decision = {}
        if isinstance(value, str) and value.startswith("分歧待裁决："):
            topic = value.removeprefix("分歧待裁决：").strip()
            for index, disagreement in enumerate(aggregation.get("disagreements") or [], start=1):
                if str(disagreement.get("topic") or "").strip() == topic:
                    decision = decisions.get(f"consensus_item:X-{index:02d}") or {}
                    break
        if decision.get("action") in ("reject", "uncertain", "request_more_info"):
            continue
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            value = str(decision["after"]).strip()
        effective_questions.append(value)
    aggregation["open_questions"] = effective_questions
    effective_consensus = []
    for index, item in enumerate(aggregation.get("consensus") or [], start=1):
        decision = decisions.get(f"consensus_item:C-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(item)
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["example"] = str(decision["after"]).strip()
            updated["topic"] = str(decision["after"]).strip()
        effective_consensus.append(updated)
    aggregation["consensus"] = effective_consensus

    effective_disagreements = []
    for index, item in enumerate(aggregation.get("disagreements") or [], start=1):
        decision = decisions.get(f"consensus_item:X-{index:02d}") or {}
        if decision.get("action") == "reject":
            continue
        updated = dict(item, disagreement_id=item.get("disagreement_id") or f"X-{index:02d}")
        if decision.get("action") == "modify" and str(decision.get("after") or "").strip():
            updated["reason"] = str(decision["after"]).strip()
        effective_disagreements.append(updated)
    aggregation["disagreements"] = effective_disagreements
    return reviewed


# 患者最终报告里每个科室最多摘取的评估条数与待确认条数：完整分析仍保留在医生端，
# 患者报告只呈现医生已审核意见中的要点，避免六个科室的长段原文堆成一页说明书。
FINAL_REPORT_OPINION_LIMIT = 2
FINAL_REPORT_CHECK_LIMIT = 2
FINAL_REPORT_FOLLOW_UP_QUESTION_LIMIT = 4
_CHECK_PREFIXES = ("建议向医生确认是否需要", "建议向医生确认", "建议确认")
FINAL_REPORT_ROUTE_LABELS = {
    "recommend_mdt": "多学科会诊（MDT）",
    "single_specialty": "单专科处理",
    "need_more_info": "补充资料后复核",
    "urgent_care": "急症优先处理",
}
_EMPTY_RECORD_VALUES = {"", "无", "（无）", "暂无", "没有", "-", "—", "（未记录）"}


def _sentence(text: Any) -> str:
    """去掉条目末尾的分隔标点，避免拼接后出现“。；”之类的重复标点。"""
    return str(text or "").strip().rstrip("。；;，, ")


def _check_question(text: Any) -> str:
    """把“建议向医生确认是否需要××”压成“是否需要××”，报告标题已说明向医生确认。"""
    value = _sentence(text)
    for prefix in _CHECK_PREFIXES:
        if value.startswith(prefix):
            rest = value[len(prefix):].strip("：: ")
            if not rest:
                return value
            return rest if rest.startswith("是否") else f"是否需要{rest}"
    return value


def _meaningful(text: Any) -> str:
    value = str(text or "").strip()
    return "" if value in _EMPTY_RECORD_VALUES else value


def _effective_specialty_opinions(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把当前轮次的专科 AI 意见投影为医生审核后的患者可读结论。

    该投影只读当前决定，不修改病例中保留的 AI 原文；驳回意见不进入患者报告，
    修改意见使用医生改写后的全文，接受意见只摘取医生已审核内容中的评估要点与
    待确认事项（信息缺口、证据出处等内部材料留在医生端完整报告）。
    """
    case = case or {}
    payload = case.get("payload") or {}
    analyses = list((review_analysis(payload) or {}).get("analyses") or [])
    items = build_items(case)
    decisions = _decision_map(current_decisions(items, payload.get("decisions") or []))
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for analysis in analyses:
        summary = specialty_analysis_summary(analysis)
        department = summary["department"]
        seen[department] = seen.get(department, 0) + 1
        target_id = department if seen[department] == 1 else f"{department}#{seen[department]}"
        decision = decisions.get(f"ai_analysis:{target_id}") or {}
        action = str(decision.get("action") or "")
        if action not in ("accept", "modify"):
            # 驳回不进入患者报告；doctor_confirmed 理论上不应存在未决定的卡，
            # 为避免误发布，没有医生决定的意见同样不写入患者最终结论。
            continue
        if action == "modify":
            revised = str(decision.get("after") or "").strip()
            if not revised:
                continue
            opinions, checks = [revised], []
        else:
            if summary["status"] != "ok":
                continue
            opinions = [_sentence(x) for x in summary["inferences"] if _sentence(x)]
            opinions = opinions[:FINAL_REPORT_OPINION_LIMIT]
            if not opinions and _sentence(summary.get("content")):
                opinions = [_sentence(summary["content"])]
            checks = [_check_question(x) for x in summary["checks"] if _sentence(x)]
            checks = checks[:FINAL_REPORT_CHECK_LIMIT]
        if not opinions:
            continue
        out.append({
            "科室": department,
            "专科评估意见": opinions,
            "建议向医生确认": checks,
            "审核": "医生已修改" if action == "modify" else "医生已确认",
        })
    return out


def _report_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _final_report_header(case: Dict[str, Any], opinions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """患者最终报告报头：按真实会诊意见单的基本信息栏组织，只放患者可见信息。"""
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    review = payload.get("review") or {}
    screening = payload.get("mdt_screening") or {}
    confirmation = payload.get("triage_doctor_confirmation") or {}
    formal = payload.get("mdt_formal_record") or {}
    decision = str(screening.get("decision") or "")
    is_mdt = decision == "recommend_mdt" or formal.get("status") == "completed"
    title = {"recommend_mdt": "多学科会诊意见报告",
             "single_specialty": "专科评估意见报告"}.get(decision, "多学科会诊意见报告" if is_mdt else "医生审核意见报告")

    primary = ""
    departments: List[str] = []
    if confirmation.get("status") == "confirmed":
        primary = str(confirmation.get("primary_department") or "").strip()
        departments = [str(dep).strip() for dep in (confirmation.get("secondary_departments") or [])
                       if str(dep).strip()]
    if not primary:
        triage_primary = ((payload.get("triage") or {}).get("primary") or {})
        primary = str(triage_primary.get("department") or "").strip()
    for dep in screening.get("departments") or []:
        dep = str(dep or "").strip()
        if dep and dep not in departments:
            departments.append(dep)
    for item in opinions:
        dep = str(item.get("科室") or "").strip()
        if dep and dep not in departments:
            departments.append(dep)
    departments = [dep for dep in departments if dep != primary]

    complaint = str(profile.get("chief_complaint") or case.get("main_issue") or "").strip()
    if len(complaint) > 80:
        complaint = complaint[:80].rstrip() + "…"
    age = str(profile.get("age") or "").strip()
    sex = str(profile.get("sex") or "").strip()
    version = int(case.get("report_version") or 1)
    case_id = str(case.get("case_id") or "")
    return {
        "报告名称": title,
        "报告编号": f"{case_id}-v{version}" if case_id else f"v{version}",
        "病历编号": case_id,
        "性别": sex,
        "年龄": (age if not age.isdigit() else f"{age} 岁"),
        "主诉": complaint,
        "主责科室": primary,
        "会诊科室": departments,
        "处理方式": FINAL_REPORT_ROUTE_LABELS.get(decision, ""),
        "审核医生": str(review.get("published_by") or ""),
        "报告日期": _report_datetime(review.get("published_at")),
        "报告版本": version,
    }


def build_patient_final_report(case: Dict[str, Any]) -> Report:
    """发布后的患者最终报告：报头 + 专科意见 + 会诊结论 + 医生建议 + 后续安排。

    结构对齐线下会诊意见单：先列各科意见要点，再给出（正式 MDT 时）会诊结论、
    医生最终建议和编号的后续安排；病例画像、信息缺口等就诊前准备内容不进入本报告。
    """
    case = case or {}
    payload = case.get("payload") or {}
    review = payload.get("review") or {}
    confirmed = case.get("case_state") == "doctor_confirmed" and bool(review.get("published"))
    report = Report(kind="patient_final", case_id=case.get("case_id", ""), generated_at=_now(),
                    badge="医生审核后发布" if confirmed else AI_BADGE,
                    doctor_confirmed=confirmed,
                    stage="doctor_reviewed" if confirmed else "ai_preparation",
                    report_version=int(case.get("report_version") or 1),
                    review_status="doctor_confirmed" if confirmed else "ai_unreviewed")
    opinions: List[Any] = _effective_specialty_opinions(case) if confirmed else []
    report.header = _final_report_header(case, opinions)
    if not opinions:
        opinions = ["（暂无已由医生确认的专科评估意见）"]

    follow_up: List[str] = []
    formal = payload.get("mdt_formal_record") or {}
    conclusion = ""
    if formal.get("status") == "completed":
        conclusion = _meaningful(formal.get("conclusion"))
        next_step = _meaningful(formal.get("next_step"))
        unresolved = _meaningful(formal.get("unresolved"))
        if next_step:
            follow_up.append(f"会诊后续安排：{_sentence(next_step)}")
        if unresolved:
            follow_up.append(f"未解决事项：{_sentence(unresolved)}")
    for item in (((review_analysis(payload) or {}).get("aggregation") or {}).get("disagreements") or []):
        topic = _sentence(item.get("topic"))
        if not topic:
            continue
        plan = _sentence(item.get("resolving_info")) or "待补充资料后复核"
        follow_up.append(f"未解决的不同意见：{topic}（{plan}）")
    pending = [_sentence(item) for item in (payload.get("review_pending_questions") or []) if _sentence(item)]
    for question in pending[:FINAL_REPORT_FOLLOW_UP_QUESTION_LIMIT]:
        follow_up.append(f"需进一步确认：{question}")
    if not follow_up:
        follow_up = ["按医生建议执行；如症状变化，请及时联系接诊医生"]

    sections: Dict[str, Any] = {"医生审核后的专科评估意见": opinions}
    if conclusion:
        sections["多学科会诊结论"] = conclusion
    sections["医生最终建议"] = str(review.get("final_advice") or "（医生未填写最终建议）")
    sections["后续安排"] = follow_up
    report.sections = sections
    return report


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


def patient_timeline_for_report(profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """生成患者端的病程变化列表。

    患者端时间线只回答“症状何时开始、如何变化”。检查结果和当前用药是
    病例画像中的快照，现有字段也没有检查日期或用药起止日期，不能把它们
    伪装成时间事件；它们继续留在“我的情况摘要”中。
    """
    profile = profile or {}
    out: List[Dict[str, Any]] = []
    for item in profile.get("symptoms_timeline") or []:
        symptom = str(item.get("symptom") or "").strip()
        onset = str(item.get("onset") or "").strip()
        if not symptom or not onset:
            continue
        row: Dict[str, Any] = {
            "时间": onset,
            "类型": "症状",
            "内容": symptom,
        }
        if item.get("trend"):
            row["说明"] = f"趋势：{item['trend']}"
        row["原文出处"] = _refs_text(item.get("source_refs"))
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
    disagreements = disagreements_for_report(
        aggregation.get("disagreements"), payload.get("disagreement_decisions"))
    uncertainties = uncertainties_for_report(aggregation.get("uncertainties"))
    handling = {
        "disagreements": [
            {
                "disagreement_id": item.get("分歧编号") or "",
                "topic": item.get("分歧主题") or "",
                "status": item.get("处理状态") or "待医生裁决",
                "choice": item.get("牵头医生决定") or "",
                "reason": item.get("处理理由") or "",
                "next_step": item.get("下一步") or "需补充更明确的证据后再判断",
                "reviewer": item.get("责任审核人") or "",
            }
            for item in disagreements
        ],
        "information_gaps": [
            {"item": str(item), "status": "待补充资料"}
            for item in case_relevant_gaps(payload)[:10]
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
        "information_gaps": [str(item) for item in case_relevant_gaps(payload)[:10]],
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


def disagreements_for_report(items: Any, decisions: Any = None) -> List[Dict[str, Any]]:
    decision_map = latest_disagreement_decisions(decisions or [])
    out: List[Dict[str, Any]] = []
    for index, item in enumerate(items or [], start=1):
        positions = item.get("positions") or {}
        disagreement_id = str(item.get("disagreement_id") or f"X-{index:02d}")
        decision = decision_map.get(disagreement_id) or {}
        status = str(decision.get("status") or "")
        status_label = DISAGREEMENT_STATUS_LABELS.get(status, "待牵头医生处理")
        out.append({
            "分歧编号": disagreement_id,
            "分歧主题": item.get("topic") or "",
            "各方立场": "；".join(f"{k}：{v}" for k, v in positions.items()) or "（未记录）",
            "分歧原因": item.get("reason") or "（未记录）",
            "处理状态": status_label,
            "牵头医生决定": decision.get("choice") or decision.get("resolution") or "（未记录）",
            "处理理由": decision.get("reason") or "（未记录）",
            "下一步": decision.get("next_step") or item.get("resolving_info") or "（未记录）",
            "责任审核人": decision.get("reviewer_name") or "（未记录）",
            "处理时间": decision.get("decided_at") or "",
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
    # 患者最终报告的报头（报告名称、病历编号、科室、审核医生等）；其他报告为空。
    header: Dict[str, Any] = field(default_factory=dict)

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
            **({"header": dict(self.header)} if self.header else {}),
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
    gaps = case_relevant_gaps(payload)
    decisions = _decision_map(payload.get("decisions") or [])
    confirmed = case.get("case_state") == "doctor_confirmed"

    consult_questions: List[str] = []
    for t in tasks:
        if t.get("type") == "就医咨询":
            content = str(t.get("content") or "").strip()
            if content and content not in consult_questions:
                consult_questions.append(content)
    for q in (((payload.get("mdt") or {}).get("aggregation") or {}).get("open_questions") or []):
        text = str(q).strip()
        if text and text not in consult_questions:
            consult_questions.append(text)
        if len(consult_questions) >= 6:
            break
    consult_questions = consult_questions[:6]

    report = Report(kind="summary", case_id=case.get("case_id", ""),
                    generated_at=_now(),
                    doctor_confirmed=confirmed,
                    stage="doctor_reviewed" if confirmed else "ai_preparation",
                    report_version=int(case.get("report_version") or 1),
                    review_status="doctor_confirmed" if confirmed else "ai_unreviewed")
    direction_values = [_departments_line(primary, secondary)]
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
    triage_confirmation = payload.get("triage_doctor_confirmation") or {}
    formal_mdt = payload.get("mdt_formal_record") or {}
    decisions = payload.get("decisions") or []
    effective_decisions = payload.get("_effective_decisions") or current_decisions(
        build_items(case), decisions)
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
    open_questions = [str(question) for question in (aggregation.get("open_questions") or [])]

    doctor_final_opinions = _doctor_opinions(
        [d for d in effective_decisions if d.get("target_type") != "ai_analysis"]
    ) if confirmed else []
    review = payload.get("review") or {}
    if confirmed and review.get("final_advice"):
        doctor_final_opinions.insert(0, {
            "医生最终建议": review["final_advice"],
            "发布医生": review.get("published_by") or "",
            "发布时间": review.get("published_at") or "",
        })
    if confirmed and formal_mdt.get("status") == "completed":
        doctor_final_opinions.append({
            "来源": "正式 MDT 会诊记录",
            "会诊结论": formal_mdt.get("conclusion") or "（未记录）",
            "未解决事项": formal_mdt.get("unresolved") or "（无）",
            "后续安排": formal_mdt.get("next_step") or "（未记录）",
        })
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
            "信息缺口": case_relevant_gaps(payload) or ["（无）"],
            "资料完整度": payload.get("completeness"),
            "原文直引覆盖率": payload.get("direct_quote_coverage"),
        },
        # 患者版只展示有明确病程时间的症状变化；检查/用药仍在病例画像中。
        "时间线": patient_timeline_for_report(profile),
        "分诊结果": {
            "病例类型": BEHAVIOR_LABELS.get(str(triage.get("behavior_type")), "（未判定）"),
            "牵头方向": triage_confirmation.get("primary_department") or
                (triage.get("primary") or {}).get("department") or "（未给出）",
            "协同方向": triage_confirmation.get("secondary_departments") or
                [d.get("department") for d in (triage.get("secondary") or [])] or ["（无）"],
            "决策路径": DECISION_PATH_LABELS.get(str(triage.get("decision_path")), "（未记录）"),
            "结果可靠性": reliability.get("level") or "（未评估）",
            "可靠性说明": list(reliability.get("reasons") or []) or ["（无）"],
            "是否需医生复核": _yes_no(triage.get("needs_doctor_review")),
            "规则校验说明": list(triage.get("validator_notes") or []) or ["（无）"],
        },
        "就医路径": path_steps_for_report(path.get("steps")),
        "各专科 AI 视角": specialty_views_for_report(mdt.get("analyses")),
        "共识": consensus_for_report(aggregation.get("consensus")),
        "分歧": disagreements_for_report(
            aggregation.get("disagreements"), payload.get("disagreement_decisions")),
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
        "医生最终意见": doctor_final_opinions,
        "AI 与医生意见差异": _differences(effective_decisions),
        "审核记录与版本": {
            "报告版本": case.get("report_version", 1),
            "病例状态": CASE_STATE_LABELS.get(str(case.get("case_state")), "（未记录）"),
            "审核条目数": len(decisions),
            "AI 分析状态": payload.get("ai_unavailable_reason") or
                ((payload.get("analysis") or {}).get("state") or "已生成专科分析"),
            "历史版本摘要": list(payload.get("review_history") or []),
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
            original_label = ("AI 原始专科意见" if d.get("target_type") == "ai_analysis"
                              else "AI 原意见")
            out.append({
                "条目": TARGET_TYPE_LABELS.get(str(d.get("target_type")), str(d.get("target_type"))),
                original_label: d.get("before"),
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
    patient_final = build_patient_final_report(case)
    review = (case.get("payload") or {}).get("review") or {}
    published = case.get("case_state") == "doctor_confirmed"
    return {
        "doctor_result": {
            "published": published,
            "final_advice": str(review.get("final_advice") or "") if published else "",
            "published_by": str(review.get("published_by") or "") if published else "",
            "published_at": str(review.get("published_at") or "") if published else "",
            "report_version": int(case.get("report_version") or 1),
        },
        "summary": summary.to_dict(),
        "full": full.to_dict(),
        "patient_final_report": patient_final.to_dict() if case.get("case_state") == "doctor_confirmed" else None,
        "summary_markdown": render_markdown(summary),
        "full_markdown": render_markdown(full),
    }
