"""数据结构 schema v1 —— 全系统数据结构与枚举的唯一权威源。

对应《产品开发规格书-比赛版v-competition.md》§5。

设计取舍（比赛版）：
- **纯标准库实现**（dataclass + 校验函数），不引入 pydantic 等额外依赖；
- 每个实体提供 `validate()`，校验失败抛 `SchemaError`（对应规格书"schema 校验失败即降级/阻断"）；
- 跨实体约定：所有"判断"必须带 `source_refs` 与 `evidence_state`；
  `confidence ∈ {high, medium, low}`（表示抽取/证据可靠度，**不是诊断概率**，见主文档 §2.5）。

用法：
    from schemas import PatientProfile, SchemaError

    p = PatientProfile.from_dict(payload)
    p.validate()          # 不合法抛 SchemaError
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class SchemaError(ValueError):
    """schema 校验失败。调用方应据此降级或阻断（规格书 §8）。"""


# ---------------------------------------------------------------------------
# 枚举（单一权威源）
# ---------------------------------------------------------------------------

CONFIDENCE_LEVELS = ("high", "medium", "low")

# 行为类型（规格书 §1）
BEHAVIOR_TYPES = ("A", "B", "C", "D", "E")

# 决策路径（规格书 §4.3）
DECISION_PATHS = ("rule-dominant", "model-dominant", "mixed", "refused", "degraded")

# 病例/报告状态（规格书 §7.1）
CASE_STATES = (
    "pending_materials",   # 资料待确认
    "analyzing",           # AI 分析中
    "awaiting_doctor",     # AI 分析完成 / 待医生审核
    "need_more_info",      # 退回补资料
    "doctor_reviewed",     # 医生已审阅
    "doctor_confirmed",    # 医生已确认
    "awaiting_recheck",    # 新资料进入后待复核
)

CASE_STATE_LABELS: Dict[str, str] = {
    "pending_materials": "资料待确认",
    "analyzing": "AI 分析中",
    "awaiting_doctor": "AI 分析完成 / 待医生审核",
    "need_more_info": "退回补资料",
    "doctor_reviewed": "医生已审阅",
    "doctor_confirmed": "医生已确认",
    "awaiting_recheck": "新资料进入后待复核",
}

# 医生 MDT 初筛子状态（独立于病例主状态机）。
MDT_SCREENING_DECISIONS = (
    "single_specialty",
    "recommend_mdt",
    "need_more_info",
    "urgent_care",
)
MDT_SCREENING_LABELS: Dict[str, str] = {
    "single_specialty": "单专科处理",
    "recommend_mdt": "建议 MDT",
    "need_more_info": "需要补充资料",
    "urgent_care": "急症处理",
}

# 正式 MDT 会诊记录状态；与 AI 预分析和医生初筛保持独立。
MDT_FORMAL_RECORD_STATUSES = (
    "planned",
    "in_progress",
    "completed",
)
MDT_FORMAL_RECORD_STATUS_LABELS: Dict[str, str] = {
    "planned": "待开始",
    "in_progress": "进行中",
    "completed": "已完成",
}

# 证据状态（规格书 §5 / 主文档 §9.3）
EVIDENCE_STATES = (
    "direct",            # 有明确证据
    "indirect",          # 证据间接支持
    "missing",           # 缺少证据
    "conflict",          # 与现有证据冲突
    "doctor_confirmed",  # 已被医生确认
    "awaiting_doctor",   # 等待医生确认
)

# 任务中心（规格书 §5.5）
TASK_TYPES = ("资料准备", "就医咨询", "就诊后资料更新")
TASK_STATES = ("todo", "doing", "done", "cancelled", "reopened")
TASK_PRIORITIES = ("high", "medium", "low")

# 医生审核（规格书 §5.6）
DOCTOR_ACTIONS = (
    "accept",
    "modify",
    "reject",
    "uncertain",
    "supplement_opinion",
    "request_more_info",
)
DECISION_TARGET_TYPES = (
    "ai_analysis",           # 专科 AI 分析判断
    "department_selection",  # 科室入选
    "path_step",             # 路径步骤
    "consensus_item",        # 共识/分歧条
    "patient_advice",        # 患者建议
)

# 可靠性等级（主文档 §6.5 / 规格书 §5.7）
RELIABILITY_LEVELS = ("高", "中", "低")
EVIDENCE_SUFFICIENCY = ("sufficient", "partial", "insufficient")
INFORMATION_CONSISTENCY = ("consistent", "conflict", "unable_to_compare")
REVIEW_STATUSES = ("ai_unreviewed", "doctor_reviewed", "doctor_confirmed", "need_more_info")

# 质询结果（规格书 §6.3：质询产出只能是结构化工作产物，不得是概率）
CHALLENGE_STANCES = ("补充证据", "修正观点", "维持原判断")
CHALLENGE_OUTCOMES = ("维持原判断", "修正观点", "需补充证据", "需医生裁决")

# 指令式医疗行为（任务文案红线：只能"建议向医生确认是否需要 XX"）
_IMPERATIVE_MEDICAL = re.compile(
    r"(立即|马上|立刻|必须|务必|自行)\s*"
    r"(停[药用]?|停药|服用|口服|注射|输液|加量|减量|换药|手术|拔管)"
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)


def _require_enum(value: Any, allowed: tuple, label: str) -> None:
    _require(value in allowed, f"{label} 取值非法: {value!r}（允许: {', '.join(map(str, allowed))}）")


# ---------------------------------------------------------------------------
# 5.1 PatientProfile（病例画像）
# ---------------------------------------------------------------------------

@dataclass
class Disease:
    name: str
    duration: str = ""
    control: str = ""
    confidence: str = "medium"
    source_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.name.strip()), "disease.name 不能为空")
        _require_enum(self.confidence, CONFIDENCE_LEVELS, "disease.confidence")
        _require(bool(self.source_refs), f"疾病「{self.name}」缺少 source_refs（必须可追溯到原文）")


@dataclass
class SymptomEvent:
    symptom: str
    onset: str = ""
    trend: str = ""
    confidence: str = "medium"
    source_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.symptom.strip()), "symptom 不能为空")
        _require_enum(self.confidence, CONFIDENCE_LEVELS, "symptom.confidence")
        _require(bool(self.source_refs), f"症状「{self.symptom}」缺少 source_refs")


@dataclass
class ExamResult:
    item: str
    value: str = ""
    abnormal: bool = False
    source_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.item.strip()), "exam.item 不能为空")
        _require(bool(self.source_refs), f"检查「{self.item}」缺少 source_refs")


@dataclass
class Medication:
    name: str
    dose: str = ""

    def validate(self) -> None:
        _require(bool(self.name.strip()), "medication.name 不能为空")


@dataclass
class PatientProfile:
    """病例画像。key 与规格书 §5.1 JSON 样例一致。"""

    case_id: str
    behavior_type: str = ""
    age: Optional[int] = None
    sex: str = ""
    chief_complaint: str = ""
    diseases: List[Disease] = field(default_factory=list)
    symptoms_timeline: List[SymptomEvent] = field(default_factory=list)
    exams: List[ExamResult] = field(default_factory=list)
    medications: List[Medication] = field(default_factory=list)
    doctor_opinions: List[str] = field(default_factory=list)
    information_gaps: List[str] = field(default_factory=list)
    completeness: float = 0.0            # 关键字段填充率（规格书 §5.1，≥0.6 放行）
    direct_quote_coverage: float = 0.0   # 原文直引覆盖率（G4 阈值 0.60）

    # 门槛常量（与规格书一致，供门禁逻辑引用）
    MIN_COMPLETENESS = 0.60
    MIN_DIRECT_QUOTE_COVERAGE = 0.60

    def validate(self) -> None:
        _require(bool(self.case_id.strip()), "case_id 不能为空")
        if self.behavior_type:
            _require_enum(self.behavior_type, BEHAVIOR_TYPES, "behavior_type")
        if self.age is not None:
            _require(0 < int(self.age) < 130, f"age 超出合理范围: {self.age}")
        _require_enum(self.sex, ("", "男", "女", "其他", "未知"), "sex")
        _require(0.0 <= self.completeness <= 1.0, "completeness 必须在 [0,1]")
        _require(0.0 <= self.direct_quote_coverage <= 1.0, "direct_quote_coverage 必须在 [0,1]")
        for d in self.diseases:
            d.validate()
        for s in self.symptoms_timeline:
            s.validate()
        for e in self.exams:
            e.validate()
        for m in self.medications:
            m.validate()

    @property
    def passes_gate(self) -> bool:
        """画像门禁（规格书 G4）：直引覆盖率不足则阻断进入分析。"""
        return self.direct_quote_coverage >= self.MIN_DIRECT_QUOTE_COVERAGE

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PatientProfile":
        return cls(
            case_id=str(data.get("case_id", "")),
            behavior_type=data.get("behavior_type", "") or "",
            age=data.get("age"),
            sex=data.get("sex", "") or "",
            chief_complaint=data.get("chief_complaint", "") or "",
            diseases=[Disease(**d) for d in data.get("diseases", [])],
            symptoms_timeline=[SymptomEvent(**s) for s in data.get("symptoms_timeline", [])],
            exams=[ExamResult(**e) for e in data.get("exams", [])],
            medications=[Medication(**m) for m in data.get("medications", [])],
            doctor_opinions=list(data.get("doctor_opinions", [])),
            information_gaps=list(data.get("information_gaps", [])),
            completeness=float(data.get("completeness", 0.0)),
            direct_quote_coverage=float(data.get("direct_quote_coverage", 0.0)),
        )


# ---------------------------------------------------------------------------
# 5.2 TriageResult（分诊结果）
# ---------------------------------------------------------------------------

@dataclass
class DepartmentPick:
    code: str
    name: str
    role: str = "协同"          # 牵头 / 协同 / 可咨询
    reason: str = ""
    signals: List[str] = field(default_factory=list)   # S1 命中的具体信号（可解释）
    confidence: Optional[float] = None                 # S2 自报（仅内部用，不出现在 UI）

    def validate(self) -> None:
        _require(bool(self.name.strip()), "department.name 不能为空")
        _require_enum(self.role, ("牵头", "协同", "可咨询"), "department.role")
        if self.confidence is not None:
            _require(0.0 <= float(self.confidence) <= 1.0, "confidence 必须在 [0,1]")


@dataclass
class Reliability:
    """可靠性等级：必须带原因（主文档 §6.5）。"""

    level: str
    reasons: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require_enum(self.level, RELIABILITY_LEVELS, "reliability.level")
        _require(bool(self.reasons), "可靠性等级必须给出原因（不得只给等级）")


@dataclass
class TriageResult:
    case_id: str
    behavior_type: str = ""
    primary_department: Optional[DepartmentPick] = None
    secondary_departments: List[DepartmentPick] = field(default_factory=list)
    reliability: Optional[Reliability] = None
    needs_doctor_review: bool = True
    decision_path: str = "mixed"
    conflicts: List[Dict[str, Any]] = field(default_factory=list)  # conflict_log（不静默）
    max_departments: int = 6                                       # 规格书校验器：总数 ≤6

    def validate(self) -> None:
        _require(bool(self.case_id.strip()), "case_id 不能为空")
        _require_enum(self.behavior_type, BEHAVIOR_TYPES, "behavior_type")
        _require_enum(self.decision_path, DECISION_PATHS, "decision_path")
        if self.primary_department:
            self.primary_department.validate()
            _require(self.primary_department.role == "牵头",
                     "primary_department.role 必须是「牵头」")
        for d in self.secondary_departments:
            d.validate()
        _require(self.total_departments <= self.max_departments,
                 f"入选科室总数 {self.total_departments} 超过上限 {self.max_departments}")
        if self.reliability:
            self.reliability.validate()

    @property
    def total_departments(self) -> int:
        return len(self.secondary_departments) + (1 if self.primary_department else 0)


# ---------------------------------------------------------------------------
# 5.3 SpecialistOpinionCard（专科意见卡）
# ---------------------------------------------------------------------------

@dataclass
class Inference:
    text: str
    confidence: str = "medium"
    evidence_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.text.strip()), "inference.text 不能为空")
        _require_enum(self.confidence, CONFIDENCE_LEVELS, "inference.confidence")


@dataclass
class CheckItem:
    priority: str
    content: str

    def validate(self) -> None:
        _require_enum(self.priority, TASK_PRIORITIES, "check.priority")
        _require(bool(self.content.strip()), "check.content 不能为空")


@dataclass
class SpecialistOpinionCard:
    case_id: str
    specialty_code: str
    specialty_name: str
    focus: str = ""
    facts: List[str] = field(default_factory=list)
    inferences: List[Inference] = field(default_factory=list)
    checks: List[CheckItem] = field(default_factory=list)
    information_gaps: List[str] = field(default_factory=list)
    referral: Optional[str] = None      # 越界转诊方向（不为空即"越界"）
    boundary_ok: bool = True            # 是否守住了本科边界
    state: str = "draft"                # draft / awaiting_doctor / confirmed / rejected
    evidence_refs: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.case_id.strip()), "case_id 不能为空")
        _require(bool(self.specialty_name.strip()), "specialty_name 不能为空")
        _require(bool(self.focus or self.facts or self.inferences or self.checks
                      or self.information_gaps), "专科意见卡不能为空")
        for inf in self.inferences:
            inf.validate()
        for chk in self.checks:
            chk.validate()
        if self.referral is not None:
            _require(bool(str(self.referral).strip()),
                     "referral 存在时必须给出具体转诊方向（越界应转诊，不得硬答）")


# ---------------------------------------------------------------------------
# 5.4 MDTAggregation（共识 / 分歧 / 不确定性 / 质询）
# ---------------------------------------------------------------------------

@dataclass
class Disagreement:
    topic: str
    positions: Dict[str, str] = field(default_factory=dict)  # 科室 → 观点
    reason: str = ""
    resolving_info: str = ""                                  # 哪项新信息可能解决
    evidence_state: str = "conflict"

    def validate(self) -> None:
        _require(bool(self.topic.strip()), "disagreement.topic 不能为空")
        _require(len(self.positions) >= 2, "分歧至少需要两个科室的不同立场")
        _require_enum(self.evidence_state, EVIDENCE_STATES, "disagreement.evidence_state")


@dataclass
class ChallengeRound:
    """轻量质询（规格书 §6.3）。产出只能是结构化工作产物，**不得是概率**。"""

    question: str
    raised_by: str = ""                       # 通常为牵头科视角
    responses: List[Dict[str, str]] = field(default_factory=list)  # [{specialty, stance, reason}]
    outcome: str = "需医生裁决"
    follow_up_evidence: List[str] = field(default_factory=list)

    def validate(self) -> None:
        _require(bool(self.question.strip()), "challenge.question 不能为空")
        _require_enum(self.outcome, CHALLENGE_OUTCOMES, "challenge.outcome")
        for r in self.responses:
            _require_enum(r.get("stance", ""), CHALLENGE_STANCES, "challenge.response.stance")


@dataclass
class MDTAggregation:
    case_id: str
    consensus: List[Dict[str, Any]] = field(default_factory=list)       # [{topic, supporters}]
    disagreements: List[Disagreement] = field(default_factory=list)
    uncertainties: List[Dict[str, Any]] = field(default_factory=list)   # [{topic, impact}]
    challenges: List[ChallengeRound] = field(default_factory=list)
    open_questions: List[str] = field(default_factory=list)             # 待医生确认的问题清单

    def validate(self) -> None:
        _require(bool(self.case_id.strip()), "case_id 不能为空")
        for d in self.disagreements:
            d.validate()
        for c in self.challenges:
            c.validate()


# ---------------------------------------------------------------------------
# 5.5 TaskItem（Action Center）
# ---------------------------------------------------------------------------

@dataclass
class TaskItem:
    task_id: str
    case_id: str
    type: str
    content: str
    source: str = ""
    priority: str = "medium"
    depends_on: List[str] = field(default_factory=list)
    state: str = "todo"
    created_at: str = ""
    completed_at: Optional[str] = None

    def validate(self) -> None:
        _require(bool(self.task_id.strip()), "task_id 不能为空")
        _require(bool(self.case_id.strip()), "case_id 不能为空")
        _require_enum(self.type, TASK_TYPES, "task.type")
        _require_enum(self.state, TASK_STATES, "task.state")
        _require_enum(self.priority, TASK_PRIORITIES, "task.priority")
        _require(bool(self.content.strip()), "task.content 不能为空")
        # 医疗行为类任务只能是"建议向医生确认"，不得是指令式
        _require(not _IMPERATIVE_MEDICAL.search(self.content),
                 f"任务内容不得出现指令式医疗行为（命中「{self.content}」）；"
                 f"只能写成「建议向医生确认是否需要 …」")


# ---------------------------------------------------------------------------
# 5.6 DoctorDecision（医生审核记录）
# ---------------------------------------------------------------------------

@dataclass
class Reviewer:
    role: str = "模拟审核员"
    dept: str = ""

    def validate(self) -> None:
        _require(bool(self.role.strip()), "reviewer.role 不能为空")


@dataclass
class DoctorDecision:
    decision_id: str
    case_id: str
    target_type: str
    target_id: str
    action: str
    before: Optional[str] = None
    after: Optional[str] = None
    reason: str = ""
    reviewer: Optional[Reviewer] = None
    evidence_refs: List[str] = field(default_factory=list)
    version: int = 1
    # 审核决定针对的内容修订号；缺失的旧记录按第 1 版兼容读取。
    content_revision: int = 1
    created_at: str = ""

    def validate(self) -> None:
        _require(bool(self.decision_id.strip()), "decision_id 不能为空")
        _require_enum(self.target_type, DECISION_TARGET_TYPES, "target_type")
        _require_enum(self.action, DOCTOR_ACTIONS, "action")
        _require(int(self.version) >= 1, "version 必须 ≥1")
        _require(int(self.content_revision) >= 1, "content_revision 必须 ≥1")
        if self.reviewer:
            self.reviewer.validate()
        # 修改/驳回必须给出理由与前后内容（规格书 §5.6）
        if self.action == "modify":
            _require(bool(self.reason.strip()), "「修改」必须填写原因")
            _require(self.before is not None and self.after is not None,
                     "「修改」必须记录修改前与修改后内容")
        if self.action == "reject":
            _require(bool(self.reason.strip()), "「驳回」必须填写原因")


# ---------------------------------------------------------------------------
# 5.7 ReliabilityLevel 综合规则（主文档 §6.5 落地）
# ---------------------------------------------------------------------------

def reliability_level(
    *,
    completeness: float,
    has_conflict: bool = False,
    only_model_inference: bool = False,
    multi_module_consistent: bool = True,
    doctor_confirmed: bool = False,
) -> Reliability:
    """按证据与完整度综合给出可靠性等级，并**必定附带原因**。

    注意：模型自报置信度**不参与**该计算（主文档 §2.5 红线）。
    """
    reasons: List[str] = []

    if only_model_inference:
        reasons.append("仅为模型推断，缺少原始证据支持")
        return Reliability(level="低", reasons=reasons)

    if has_conflict:
        reasons.append("存在证据冲突或专科分歧未收敛")
        return Reliability(level="低", reasons=reasons)

    if completeness < PatientProfile.MIN_COMPLETENESS:
        reasons.append(f"关键字段完整度不足（{completeness:.2f} < {PatientProfile.MIN_COMPLETENESS}）")
        return Reliability(level="中", reasons=reasons)

    if not multi_module_consistent:
        reasons.append("多模块结论不完全一致")
        return Reliability(level="中", reasons=reasons)

    reasons.append("关键资料完整、无冲突、多模块结论一致")
    if doctor_confirmed:
        reasons.append("人工审核已完成，但不改变证据充分度")
    return Reliability(level="高", reasons=reasons)
