"""就诊后资料更新与变化摘要（规格书 §7 回路 / 任务 3.6）。

职责：
1. **患者补充自己手上有的新资料**（新检查、新处方、症状变化、复查安排）；
2. **医生端录入线下实际意见**（由具备审核权限的医生执行）——两类来源分开处理：
   患者上传内容**永不自动**成为医生确认意见（主文档 §14）；
3. 用**依赖矩阵**确定受影响模块 → **只重算分诊/路径等受影响部分**（增量原则）；
4. 生成**变化摘要**：新增了什么 / 哪些判断变了 / 哪些未受影响 / 任务变化 / 需重新确认。

红线：变化摘要只描述"哪些判断被质疑、缺什么证据、是否维持"，**不输出概率数字**。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.privacy import desensitize  # noqa: E402
from services.dependency_matrix import departments_for_field  # noqa: E402

# 来源分级（主文档 §14）
SOURCE_PATIENT = "患者上传"
SOURCE_DOCTOR = "医生上传"
SOURCE_HOSPITAL = "医院文档"
SOURCE_UNVERIFIED = "未验证资料"
SOURCE_CONFIRMED = "已确认资料"

VALID_SOURCES = (SOURCE_PATIENT, SOURCE_DOCTOR, SOURCE_HOSPITAL,
                 SOURCE_UNVERIFIED, SOURCE_CONFIRMED)

# 单条更新与首次病历录入保持同一数量级限制；拒绝超长内容而不是在合并病史时
# 静默截断，避免把关键检查结果截掉后仍当成完整资料分析。
MAX_UPDATE_CONTENT_LENGTH = 8000
MAX_MERGED_HISTORY_LENGTH = 16000

# 更新内容类别 → 依赖矩阵字段（用于确定受影响模块）
UPDATE_KIND_FIELDS: Dict[str, tuple] = {
    "检查结果": ("urine_protein", "blood_glucose_control", "fundus_exam",
               "cardiac_exam", "blood_count", "imaging", "renal_function"),
    "用药方案": ("medication_list",),
    "症状变化": ("symptom_trend", "symptom_duration"),
    "复查安排": (),
    "医生意见": (),
}


# 缺口字段名 → 能"填补"它的内容关键词（新资料若命中，则对应缺口消除）
GAP_FILL_KEYWORDS: Dict[str, tuple] = {
    "眼底检查": ("眼底", "视网膜", "散瞳", "检眼镜"),
    "血常规（三系）": ("血常规", "白细胞", "血红蛋白", "血小板", "血细胞"),
    "心脏/冠脉检查": ("心脏", "冠脉", "心电图", "心超", "超声心动", "bnp"),
    "影像检查（CT/MRI）": ("ct", "mri", "核磁", "影像", "胸片", "超声"),
    "尿蛋白 / 肾功能": ("尿蛋白", "肌酐", "肾功能", "egfr", "尿检"),
    "血糖控制水平": ("血糖", "糖化", "空腹血糖", "餐后血糖"),
    "甲状腺功能": ("甲功", "tsh", "t3", "t4"),
    "肝功能": ("肝功能", "转氨酶", "胆红素"),
    "过敏史": ("过敏",),
    "近期体重变化": ("体重",),
    "当前用药清单": ("用药", "服药", "口服", "医嘱", "mg", "片"),
    "既往疾病": ("既往", "病史", "诊断"),
}


def gaps_filled_by(content: str) -> List[str]:
    """判断这次更新填补了哪些缺口（按关键词匹配矩阵标准字段名）。

    修正缺陷：原来新资料录入后缺口列表不变，导致"已提供眼底检查"却仍显示
    "眼底检查：信息不足"。
    """
    text = str(content or "").lower()
    filled: List[str] = []
    for label, keywords in GAP_FILL_KEYWORDS.items():
        if any(k.lower() in text for k in keywords):
            filled.append(label)
    return filled


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class UpdateRecord:
    """一条就诊后资料更新。"""

    update_id: str
    case_id: str
    kind: str                       # 检查结果 / 用药方案 / 症状变化 / 复查安排 / 医生意见
    content: str
    source: str = SOURCE_PATIENT
    at: str = ""
    affected_departments: List[str] = field(default_factory=list)
    affected_modules: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "update_id": self.update_id, "case_id": self.case_id, "kind": self.kind,
            "content": self.content, "source": self.source, "at": self.at,
            "source_label": self.source, "is_verified": self.source in (SOURCE_CONFIRMED, SOURCE_DOCTOR),
            "affected_departments": list(self.affected_departments),
            "affected_modules": list(self.affected_modules),
        }


@dataclass
class ChangeSummary:
    """变化摘要（P11）。"""

    case_id: str = ""
    generated_at: str = ""
    added: List[str] = field(default_factory=list)            # 新增了什么
    changed: List[str] = field(default_factory=list)          # 哪些判断变了
    unchanged: List[str] = field(default_factory=list)        # 哪些未受影响
    task_changes: List[str] = field(default_factory=list)     # 任务变化
    needs_doctor_review: List[str] = field(default_factory=list)
    doctor_opinion_comparison: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """是否有任何"判断变化"（前端决定是否提示"结果已更新"）。"""
        return bool(self.added or self.changed or self.task_changes)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "generated_at": self.generated_at,
            "added": list(self.added),
            "changed": list(self.changed),
            "unchanged": list(self.unchanged),
            "task_changes": list(self.task_changes),
            "needs_doctor_review": list(self.needs_doctor_review),
            "doctor_opinion_comparison": dict(self.doctor_opinion_comparison),
            "notes": list(self.notes),
            "has_changes": self.has_changes,
        }


def determine_affected(kind: str, content: str,
                       current_departments: Sequence[str]) -> Dict[str, List[str]]:
    """用依赖矩阵判断：这次更新影响哪些字段与专科模块。"""
    fields: List[str] = list(UPDATE_KIND_FIELDS.get(kind, ()))
    if not fields:
        return {"fields": [], "departments": [], "modules": []}

    departments: List[str] = []
    for field in fields:
        for dep in departments_for_field(field):
            if dep not in departments:
                departments.append(dep)

    # 只保留当前参与科室（其他科室本次不涉及）
    affected = [d for d in departments if d in (current_departments or [])]
    modules: List[str] = []
    if affected:
        modules.append("分诊与路径")
        modules.append("多专科分析（受影响科室：" + "、".join(affected) + "）")
    return {"fields": fields, "departments": affected, "modules": modules}


def build_change_summary(
    *,
    case_id: str,
    old_triage: Optional[Dict[str, Any]],
    new_triage: Optional[Dict[str, Any]],
    old_path: Optional[Dict[str, Any]],
    new_path: Optional[Dict[str, Any]],
    updates: Sequence[UpdateRecord],
    unresolved_gaps: Sequence[str] = (),
    doctor_opinions: Optional[Sequence[Dict[str, Any]]] = None,
) -> ChangeSummary:
    """对比更新前后的分诊与路径，生成变化摘要。"""
    summary = ChangeSummary(case_id=case_id, generated_at=_now())

    # ---- 新增了什么 ----
    for record in updates:
        label = record.source
        summary.added.append(f"[{label}] {record.kind}：{record.content[:80]}")

    old_triage = old_triage or {}
    new_triage = new_triage or {}

    # ---- 哪些判断变了（科室/牵头/可靠性） ----
    old_primary = (old_triage.get("primary") or {}).get("department")
    new_primary = (new_triage.get("primary") or {}).get("department")
    if old_primary != new_primary:
        summary.changed.append(f"牵头方向：{old_primary or '（无）'} → {new_primary or '（无）'}")

    old_sel = set(old_triage.get("selected") or [])
    new_sel = set(new_triage.get("selected") or [])
    for dep in sorted(new_sel - old_sel):
        summary.changed.append(f"新增建议科室：{dep}")
    for dep in sorted(old_sel - new_sel):
        summary.changed.append(f"移除建议科室：{dep}")

    old_rel = (old_triage.get("reliability") or {}).get("level")
    new_rel = (new_triage.get("reliability") or {}).get("level")
    if old_rel != new_rel:
        summary.changed.append(f"结果可靠性：{old_rel or '—'} → {new_rel or '—'}")

    old_path_steps = {s.get("step_id") for s in (old_path or {}).get("steps") or []}
    new_path_steps = {s.get("step_id") for s in (new_path or {}).get("steps") or []}
    if old_path_steps != new_path_steps:
        summary.changed.append(
            f"就医路径步骤数：{len(old_path_steps)} → {len(new_path_steps)}")

    # ---- 哪些未受影响 ----
    for dep in sorted(new_sel & old_sel):
        summary.unchanged.append(f"{dep} 的参与判断未受影响")
    if not summary.unchanged:
        summary.unchanged.append("（本次更新影响了全部参与科室）")

    # ---- 任务变化 ----
    old_tasks = {t.get("content"): t for t in (old_path or {}).get("tasks") or []}
    new_tasks = {t.get("content"): t for t in (new_path or {}).get("tasks") or []}
    for content in new_tasks:
        if content not in old_tasks:
            summary.task_changes.append(f"新增任务：{content}")
    for content in old_tasks:
        if content not in new_tasks:
            summary.task_changes.append(f"取消任务：{content}")
    for content, task in new_tasks.items():
        old_task = old_tasks.get(content)
        if old_task and old_task.get("priority") != task.get("priority"):
            summary.task_changes.append(
                f"任务优先级变化：{content}（{old_task.get('priority')} → {task.get('priority')}）")

    # ---- 需医生重新确认 ----
    if summary.changed:
        summary.needs_doctor_review.append("因本次更新导致判断变化，需医生重新确认")
    for gap in list(unresolved_gaps)[:3]:
        summary.needs_doctor_review.append(f"仍未确认：{gap}")

    # ---- AI 建议 vs 医生意见对照（若医生端已录入线下意见） ----
    if doctor_opinions:
        # 只有医生端/已确认来源的意见才算"医生意见"；患者转述的单独列出（红线：不覆盖医生）
        verified = [i for i in doctor_opinions if i.get("is_verified")]
        unverified = [i for i in doctor_opinions if not i.get("is_verified")]
        agree, differ = [], []
        for item in verified:
            opinion_dept = str(item.get("department") or "").strip()
            if opinion_dept and opinion_dept in new_sel:
                agree.append(opinion_dept)
            elif opinion_dept:
                differ.append({"department": opinion_dept,
                               "note": item.get("opinion") or item.get("content") or "",
                               "type": "科室建议差异"})
        summary.doctor_opinion_comparison = {
            "agree": agree,
            "differences": differ,
            "unverified": [{"department": i.get("department"),
                            "opinion": i.get("opinion") or i.get("content") or ""}
                           for i in unverified],
            "note": "AI 建议与线下医生意见如实并列，AI 绝不覆盖医生判断；"
                    "患者转述内容不作为医生确认意见。",
        }
        if unverified:
            summary.needs_doctor_review.append(
                "患者转述的线下医生意见尚未经医生端确认，暂不作为医生意见采信")

    summary.notes.append(
        "变化摘要只说明「新资料改变了什么」，不改变医生最终判断权；"
        "未受影响的模块未重新分析。")
    return summary


def build_doctor_opinion(department: str, opinion: str,
                         source: str = SOURCE_DOCTOR) -> Dict[str, Any]:
    """构造一条"线下医生实际意见"（由医生端录入，而非患者代填）。"""
    if source not in VALID_SOURCES:
        source = SOURCE_DOCTOR
    return {"department": department, "opinion": opinion,
            "source": source, "at": _now(),
            "is_verified": source in (SOURCE_CONFIRMED, SOURCE_DOCTOR)}


def sanitize_update_content(content: str, *, field_name: str = "更新内容") -> str:
    """校验并返回只含脱敏文本的更新内容。

    更新资料会进入病例 JSON、变化摘要、医生意见列表以及后续分析文本，不能
    复用“预览时提示即可”的策略：可直接定位个人的 PII 必须拒绝，姓名/住址等
    非阻断型 PII 则自动打码后再继续。错误信息只返回类型，不回显命中的原文。
    """
    if not isinstance(content, str):
        raise ValueError(f"{field_name}必须是字符串")
    text = content.strip()
    if not text:
        raise ValueError(f"{field_name}不能为空")
    if len(text) > MAX_UPDATE_CONTENT_LENGTH:
        raise ValueError(
            f"{field_name}过长（最多 {MAX_UPDATE_CONTENT_LENGTH} 个字符），请分批录入"
        )
    pii = desensitize(text)
    if pii.blocked:
        kinds = "、".join(sorted({hit.label for hit in pii.blocking_hits}))
        raise ValueError(
            f"{field_name}包含需先清除的敏感信息（{kinds}），请清除后再提交"
        )
    return pii.masked_text


def sanitize_optional_content(content: Optional[str], *, field_name: str) -> str:
    """允许空白的可选文本字段，但非字符串仍然拒绝。"""
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ValueError(f"{field_name}必须是字符串")
    if not content.strip():
        return ""
    return sanitize_update_content(content, field_name=field_name)


def validate_update(kind: str, content: str, source: str) -> None:
    """校验一条更新（空内容、非法来源直接拒绝）。"""
    if not isinstance(kind, str):
        raise ValueError("更新类别必须是字符串")
    if kind not in UPDATE_KIND_FIELDS:
        raise ValueError(f"未知更新类别（可选：{'、'.join(UPDATE_KIND_FIELDS)}）")
    # 只做一次完整校验；调用方如需打码文本应使用 sanitize_update_content。
    sanitize_update_content(content)
    if not isinstance(source, str):
        raise ValueError("更新来源必须是字符串")
    if source not in VALID_SOURCES:
        raise ValueError(f"未知更新来源（可选：{'、'.join(VALID_SOURCES)}）")
