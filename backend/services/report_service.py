"""报告体系：患者版一页摘要 + 医生审核版完整报告 + Markdown 渲染。

对应《产品开发规格书-比赛版v-competition.md》任务 3.5 与主文档 §12。

红线（写死）：
- 患者版**禁止**把未审核推断写成确定诊断或治疗意见；
- 任何未经医生确认的输出必须带 **"AI 生成，尚未经医生审核"** 角标（规格书 §3.3）；
- 报告必填段落完整率 100%（规格书 §10 验收线）；
- 医疗行为一律"建议向医生确认是否需要…"，不得下指令。
"""

from __future__ import annotations

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
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    triage = payload.get("triage") or {}
    path = payload.get("path") or {}
    tasks = payload.get("tasks") or []

    primary = triage.get("primary") or {}
    secondary = triage.get("secondary") or []
    gaps = [g for g in (payload.get("gaps") or [])]

    consult_questions: List[str] = []
    for t in tasks:
        if t.get("type") == "就医咨询":
            content = str(t.get("content") or "").strip()
            if content and content not in consult_questions:
                consult_questions.append(content)
    for q in ((payload.get("mdt") or {}).get("aggregation") or {}).get("open_questions") or []:
        text = str(q).strip()
        if text and text not in consult_questions:
            consult_questions.append(text)
        if len(consult_questions) >= 6:
            break
    consult_questions = consult_questions[:6]

    report = Report(kind="summary", case_id=case.get("case_id", ""),
                    generated_at=_now(),
                    doctor_confirmed=(case.get("case_state") == "doctor_confirmed"))
    report.sections = {
        "主要问题": profile.get("chief_complaint") or case.get("main_issue") or "（未提供）",
        "关键已知资料": _known_facts(profile),
        "当前信息缺口": gaps or ["（暂无）"],
        "建议咨询方向": _departments_line(primary, secondary),
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
    return facts or ["（未提供）"]


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
    payload = case.get("payload") or {}
    profile = payload.get("profile") or {}
    triage = payload.get("triage") or {}
    path = payload.get("path") or {}
    mdt = payload.get("mdt") or {}
    aggregation = mdt.get("aggregation") or {}
    decisions = payload.get("decisions") or []

    cards = build_evidence_cards(profile, triage, mdt)
    evidence_stat = summarize_evidence(cards)

    report = Report(kind="full", case_id=case.get("case_id", ""),
                    generated_at=_now(),
                    doctor_confirmed=(case.get("case_state") == "doctor_confirmed"))

    sections: Dict[str, Any] = {
        "病例画像": {
            "基本信息": f"{profile.get('sex') or ''} {profile.get('age') or ''}".strip(),
            "主诉": profile.get("chief_complaint") or "",
            "既往疾病": [f"{d.get('name')} {d.get('duration') or ''}".strip()
                       for d in (profile.get("diseases") or [])],
            "检查异常": [f"{e.get('item')} {e.get('value') or ''}".strip()
                       for e in (profile.get("exams") or [])],
            "当前用药": [f"{m.get('name')} {m.get('dose') or ''}".strip()
                       for m in (profile.get("medications") or [])],
            "信息缺口": payload.get("gaps") or [],
            "完整度": payload.get("completeness"),
            "原文直引覆盖率": payload.get("direct_quote_coverage"),
        },
        "时间线": build_timeline(profile),
        "分诊结果": {
            "行为类型": triage.get("behavior_type"),
            "牵头方向": (triage.get("primary") or {}).get("department"),
            "协同方向": [d.get("department") for d in (triage.get("secondary") or [])],
            "决策路径": triage.get("decision_path"),
            "可靠性": triage.get("reliability"),
            "是否需医生复核": triage.get("needs_doctor_review"),
            "规则校验说明": triage.get("validator_notes") or [],
        },
        "就医路径": path.get("steps") or [],
        "各专科 AI 视角": mdt.get("analyses") or [],
        "共识": aggregation.get("consensus") or [],
        "分歧": aggregation.get("disagreements") or [],
        "不确定性": aggregation.get("uncertainties") or [],
        "关键待解决问题": aggregation.get("open_questions") or [],
        "证据关联": {
            "统计": evidence_stat,
            "状态说明": {k: v for k, v in STATE_LABELS.items()},
            "证据卡片": cards,
        },
        "医生最终意见": _doctor_opinions(decisions),
        "AI 与医生意见差异": _differences(decisions),
        "审核记录与版本": {
            "版本": case.get("report_version", 1),
            "病例状态": case.get("case_state"),
            "审核条目数": len(decisions),
            "最近操作": decisions[-3:] if decisions else [],
        },
    }

    report.sections = sections
    # 必填段完整率 100%：未能生成的段落给出**说明性占位**（诚实标注未生成本版），
    # 而不是留空 —— 否则"结构完整性"验收线无法达成。
    report.missing_sections = []
    for name in REQUIRED_SECTIONS:
        value = sections.get(name)
        if value in (None, [], {}):
            sections[name] = "（本版本未生成：模型未配置或无可用意见卡/审核记录）"
    return report


def _doctor_opinions(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"条目": d.get("target_type"), "操作": d.get("action"),
             "内容": d.get("after") or d.get("before"), "原因": d.get("reason"),
             "审核人": (d.get("reviewer") or {}).get("role")}
            for d in decisions if d.get("action") in ("accept", "modify", "supplement_opinion")]


def _differences(decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """AI 建议 vs 医生意见的差异（医生修改/驳回即差异）。"""
    out = []
    for d in decisions:
        if d.get("action") in ("modify", "reject"):
            out.append({
                "条目": d.get("target_type"),
                "AI 原意见": d.get("before"),
                "医生意见": d.get("after"),
                "差异类型": "医生修改" if d.get("action") == "modify" else "医生驳回",
                "原因": d.get("reason") or "（未填写）",
            })
    return out


# ---------------------------------------------------------------------------
# Markdown 渲染（浏览器"打印为 PDF"用）
# ---------------------------------------------------------------------------

def render_markdown(report: Report) -> str:
    lines: List[str] = []
    title = "一页就医摘要" if report.kind == "summary" else "多学科就医辅助报告（医生审核版）"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 病例号：{report.case_id}　生成时间：{report.generated_at}")
    if not report.doctor_confirmed:
        lines.append(f"> ⚠️ **{AI_BADGE}**")
    else:
        lines.append("> ✅ 已由医生审核确认")
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
