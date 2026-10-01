"""病例流程编排（接线层）：把已实现的模块串成可用的患者端链路。

对应规格书 §2 P0 主链路的**前半段**：

    录入与校验(G1/G2/G3) → 脱敏 → 病例结构化(画像+G4门禁) → 智能追问
    →（分诊/MDT 属阶段 2，本层预留接入点）

设计原则：
- **只编排，不重复实现规则**：校验、脱敏、抽取、追问全部复用各自模块；
- **阻断优先**：G1 危象 → 直接返回急救提示，不落库、不进入后续步骤；
- **可落盘**：病例状态与审计写入 `Store`（重启后可查）；
- **不编造**：缺口与未回答的问题如实保留，交由可靠性等级体现（主文档 §5.5）。
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
import logging
from functools import wraps
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ask_service import (  # noqa: E402
    MAX_ANSWER_ITEMS,
    MAX_ANSWER_KEY_LENGTH,
    Question,
    apply_answers,
    build_questions,
)
from services import challenge_service  # noqa: E402
from services.guardrail import screen  # noqa: E402
from services.intake import IntakeResult, sanitized_payload, validate_intake  # noqa: E402
from services.mdt_analyst import METHOD_LLM_REPAIRED, aggregate, analyze_specialty  # noqa: E402
from services.path_service import build_path  # noqa: E402
from services.portrait_extractor import ExtractionResult, extract  # noqa: E402
from services.privacy import desensitize  # noqa: E402
from services import report_service  # noqa: E402
from services import review_service  # noqa: E402
from services import task_service  # noqa: E402
from services import update_service  # noqa: E402
from services import department_domain  # noqa: E402
from services.rule_engine import evaluate as evaluate_rules  # noqa: E402
from services.s2_analyst import analyze as s2_analyze  # noqa: E402
from services.store import Store  # noqa: E402
from services.triage import orchestrate  # noqa: E402
from schemas import (CASE_STATE_LABELS, CheckItem, Inference, MDT_FORMAL_RECORD_STATUS_LABELS,
                     MDT_FORMAL_RECORD_STATUSES, MDT_SCREENING_DECISIONS,
                     MDT_SCREENING_LABELS, SpecialistOpinionCard)  # noqa: E402

# 耗时控制：多专科分析最多取前 3 个科室
MAX_MDT_DEPARTMENTS = 3

MDT_SCREENING_NEXT_STEPS = {
    "single_specialty": "请按医生安排先咨询相关专科。",
    "recommend_mdt": "医生建议多学科共同评估，请按后续就医安排准备资料。",
    "need_more_info": "请按医生提示补充资料后再继续。",
    "urgent_care": "请尽快前往急诊或拨打 120，不要等待普通 MDT 流程。",
}

# 病例状态（与 schemas.CASE_STATES 保持一致）
STATE_PENDING = "pending_materials"
STATE_AWAITING_DOCTOR = "awaiting_doctor"
STATE_NEED_MORE_INFO = "need_more_info"

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def patient_safe_mdt_screening(screening: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """返回患者可见的初筛状态，不暴露审核人账号或内部审计字段。"""
    if not screening:
        return None
    allowed = (
        "status", "decision", "decision_label", "purpose", "departments",
        "patient_next_step", "mdt_queue_eligible", "decided_at",
    )
    return {key: screening[key] for key in allowed if key in screening}


def new_case_id() -> str:
    """生成病例号：#C + 8 位随机（便于脱敏展示与检索）。"""
    return "C" + uuid.uuid4().hex[:8].upper()


def serialized_case(method):
    """串行化同一病例的复合读-改-写操作，不阻塞不同病例。"""
    @wraps(method)
    def wrapped(self, case_id, *args, **kwargs):
        with self._case_lock(case_id):
            return method(self, case_id, *args, **kwargs)
    return wrapped


@dataclass
class CaseCreation:
    """创建病例的返回（对应患者端 P2-P5 屏所需数据）。"""

    ok: bool = False
    case_id: str = ""
    blocked: bool = False                 # 是否被 Guardrail 阻断（G1/G6）
    blocked_by: str = ""
    advice: str = ""                      # 阻断时的提示语（危象=急救提示）
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    masked_preview: Dict[str, str] = field(default_factory=dict)   # 脱敏预览（P3）
    profile: Optional[Dict[str, Any]] = None                        # 画像卡（P4）
    gaps: List[str] = field(default_factory=list)                   # 信息缺口
    completeness: float = 0.0
    direct_quote_coverage: float = 0.0
    passes_gate: bool = False             # G4
    questions: List[Dict[str, Any]] = field(default_factory=list)   # 追问（P5）
    case_state: str = STATE_PENDING
    goals: List[str] = field(default_factory=list)                  # 已选就医目标
    triage: Optional[Dict[str, Any]] = None                         # 分诊结果（P6）
    path: Optional[Dict[str, Any]] = None                           # 就医路径与任务（P7）
    mdt: Optional[Dict[str, Any]] = None                            # 多专科意见卡 + 聚合（P8）
    mdt_pending: bool = False                                       # 两段式：MDT 是否仍在后台跑
    progressive: bool = False                                       # 渐进式：先给规则结果
    analysis_pending: bool = False                                  # 后台分析任务是否在跑
    profile_is_rules_only: bool = False                             # 画像是否仅为规则抽取（模型增强未回）
    s2: Optional[Dict[str, Any]] = None                             # S2 受限发言记录（可解释）
    # 完整脱敏正文只供内部持久化/后续分析使用，不直接放进 API 的预览字段。
    # 这样前端仍可拿到有限长度预览，同时数据库不会把 8000 字病历截成 2000 字。
    masked_history: str = field(default="", repr=False)
    masked_main_issue: str = field(default="", repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "case_id": self.case_id, "blocked": self.blocked,
            "blocked_by": self.blocked_by, "advice": self.advice,
            "errors": list(self.errors), "warnings": list(self.warnings),
            "masked_preview": dict(self.masked_preview),
            "profile": self.profile, "gaps": list(self.gaps),
            "completeness": self.completeness,
            "direct_quote_coverage": self.direct_quote_coverage,
            "passes_gate": self.passes_gate,
            "questions": list(self.questions),
            "case_state": self.case_state,
            "goals": list(self.goals),
            "triage": self.triage,
            "path": self.path,
            "mdt": self.mdt,
            "mdt_pending": self.mdt_pending,
            "progressive": self.progressive,
            "analysis_pending": self.analysis_pending,
            "profile_is_rules_only": self.profile_is_rules_only,
            "s2": self.s2,
        }


class CaseFlow:
    """患者端病例流程编排。"""

    def __init__(self, store: Optional[Store] = None, ai_service: Any = None,
                 use_llm: bool = False) -> None:
        self.store = store
        self.ai_service = ai_service
        self.use_llm = use_llm
        self._jobs_lock = threading.Lock()
        self._active_jobs: Dict[str, threading.Thread] = {}
        self._case_locks_guard = threading.Lock()
        self._case_locks: Dict[str, threading.RLock] = {}

    def _case_lock(self, case_id: str) -> threading.RLock:
        with self._case_locks_guard:
            return self._case_locks.setdefault(case_id, threading.RLock())

    # ------------------------------------------------------------------ 脱敏预览
    def build_preview(self, medical_history: str, main_issue: str = "") -> Dict[str, Any]:
        """提交前预览（P3）：只做 Guardrail 检查 + 脱敏，**不落库、不分析**。

        返回结构供患者端渲染"哪些内容会被打码、为什么打码"。
        """
        combined = "\n".join(x for x in (medical_history or "", main_issue or "") if x)
        pii = desensitize(combined)
        guard = screen(combined)

        return {
            "blocked": guard.blocked,
            "blocked_by": guard.code,
            "advice": guard.advice,
            "reasons": guard.reasons,
            "is_crisis": guard.is_crisis,
            "masked_text": pii.masked_text,
            "hits": pii.summary(),          # 不含原文
            "hit_count": len(pii.hits),
            "blocking_hit_count": len(pii.blocking_hits),
        }

    # ------------------------------------------------------------------ 多专科分析
    def _run_mdt(self, *, text: str, outcome: Any, profile: Optional[Dict[str, Any]],
                 case_id: str, answers: Optional[Dict[str, str]] = None,
                 rejections: Optional[List[str]] = None,
                 on_department: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
        """对入选科室做**第一轮独立研判**（互不预读）并聚合（P8）。

        每个专科单独调用、提示词里不含其他科室结论 —— 防锚定（主文档 §8.1）。
        `on_department(dept, done, total)` 上报"正在分析第几科"，供接口层显示进度。
        """
        departments = outcome.selected[:MAX_MDT_DEPARTMENTS]
        skip_reason = ""
        if not departments:
            skip_reason = "没有明确的就医方向，未运行多学科会诊"
        elif len(departments) < 2:
            skip_reason = "仅 1 个就医方向，无需多学科会诊"
        elif getattr(outcome, "behavior_type", "") == "A":
            # A 型（单科为主）不硬凑 MDT：界面上写"暂不需要多学科会诊"，就不能偷偷跑
            # 旧病例可能没有 behavior_type；已有多个持久化入选科室时不能因此误跳过。
            skip_reason = "本例以单科为主（A 型），未运行多学科会诊"
        if skip_reason:
            return {
                "departments": departments,
                "analyses": [],
                "aggregation": {"consensus": [], "disagreements": [], "uncertainties": [],
                                "open_questions": [], "notes": [skip_reason]},
                "card_count": 0,
                "challenge": {"triggered": False, "rounds": [], "notes": []},
                "failed_departments": [], "repaired_departments": [], "notices": [],
                "skipped_reason": skip_reason,
            }
        analyses = []
        for index, dep in enumerate(departments, start=1):
            if on_department:
                on_department(dep, index, len(departments))
            a = analyze_specialty(
                ai_service=self.ai_service, department=dep, text=text,
                profile=profile, case_id=case_id, answers=answers,
            )
            analyses.append(a)

        cards = [a.card for a in analyses if a.card is not None]
        aggregation = aggregate(cards, rejections=rejections or [])
        # 入选方向可能多于 MDT 上限（成本控制）→ 如实说明"这次只深挖了前 N 个"
        if len(outcome.selected) > len(departments):
            aggregation.notes.append(
                f"本次多学科分析覆盖优先度最高的 {len(departments)} 个方向"
                f"（就医方向共 {len(outcome.selected)} 个，其余方向以分诊与路径结果为准）")
        failed = [a.department for a in analyses if a.card is None]
        if failed:
            aggregation.notes.append(f"以下专科分析未完成（已跳过，不阻塞）：{'、'.join(failed)}")
        # 卡片是"修复后可用"的科室也要如实标注（例：输出被长度截断，按已写出字段恢复）
        repaired = [a.department for a in analyses
                    if a.card is not None and a.method == METHOD_LLM_REPAIRED]
        if repaired:
            aggregation.notes.append(
                f"以下专科意见卡经格式修复后可用（个别字段可能缺失）：{'、'.join(repaired)}")

        # ---- 轻量质询（规格书 §6.3）：有分歧/共同缺口时才触发 ----
        agg_dict = aggregation.to_dict()
        card_reasons = {
            a.card.specialty_name: "；".join(i.text for i in a.card.inferences[:2])
            for a in analyses if a.card is not None
        }
        challenge = challenge_service.run(
            ai_service=self.ai_service, aggregation=agg_dict, cards=cards,
            text=text, card_reasons=card_reasons,
        )
        if challenge.triggered:
            # 质询结论并入聚合（供报告分歧区与医生端展示）
            agg_dict["challenges"] = challenge.to_dict()["rounds"]
            if challenge.needs_doctor_decision:
                agg_dict["open_questions"] = list(agg_dict.get("open_questions", [])) + [
                    "质询后仍未收敛的问题（详见质询记录）"
                ]
            agg_dict["notes"] = list(agg_dict.get("notes", [])) + list(challenge.notes)

        return {
            "departments": departments,
            "analyses": [a.to_dict() for a in analyses],
            "aggregation": agg_dict,
            "card_count": len(cards),
            "challenge": challenge.to_dict(),
            # 单个科室失败要能被"重试"识别到（否则相位算完成 → 永远无法重跑）
            "failed_departments": failed,
            "repaired_departments": repaired,
            "notices": [f"{a.department}：{a.error}" for a in analyses
                        if a.card is not None and a.error],
        }

    # ------------------------------------------------------------------ 创建
    # 阶段定义：与前端进度条一一对应（percent 是**阶段完成度**，不是估算的假进度）
    CREATE_STAGES: List[Tuple[str, str, int]] = [
        ("intake", "校验录入内容", 6),
        ("mask", "脱敏处理", 12),
        ("profile", "结构化病例画像", 30),
        ("questions", "生成关键追问", 38),
        ("triage", "推演就医方向（规则 → 模型 → 编排）", 52),
        ("path", "生成就医路径与任务", 60),
        ("mdt", "组织多学科独立意见（耗时主要在这里）", 94),
        ("save", "写入病例档案", 100),
    ]

    def create_case(
        self,
        *,
        medical_history: str = "",
        main_issue: str = "",
        goals: Optional[List[str]] = None,
        age: Any = None,
        sex: str = "",
        name: str = "",
        case_id: Optional[str] = None,
        owner_id: str = "",
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        defer_mdt: bool = False,
        progressive: bool = False,
    ) -> CaseCreation:
        """执行"录入 → 校验 → 脱敏 → 画像 → 追问 → 分诊 → 路径 → 多学科"整条链路。

        `on_progress`：可选回调，每完成一个阶段调用一次，参数形如
        `{"stage": "triage", "message": "推演就医方向", "percent": 52, "index": 5, "total": 8}`。
        这条链路里含 3 次模型调用（画像增强 / S2 受限发言 / 多专科意见卡 + 质询），
        实测约 10-30 秒 —— 前端据此显示**真实阶段进度**，避免用户以为卡死。

        `defer_mdt=True`：**两段式**。画像/追问/分诊/路径先返回（实测约 3 秒），
        多学科意见卡 + 质询放到后台线程继续跑（这一块实测占 26 秒以上），
        进度写进 `payload["mdt_status"]`，患者端到 P8 时再取；`get_mdt_status()` 可查询。
        """
        result = CaseCreation()
        stage_total = len(self.CREATE_STAGES)

        def _progress(stage: str) -> None:
            if not on_progress:
                return
            for index, (key, message, percent) in enumerate(self.CREATE_STAGES, start=1):
                if key == stage:
                    on_progress({"stage": key, "message": message, "percent": percent,
                                 "index": index, "total": stage_total})
                    return

        # ---- 1. 录入校验（含 G1 危象 / G2 双空 / G3 PII）----
        intake: IntakeResult = validate_intake(
            medical_history=medical_history, main_issue=main_issue,
            goals=goals, age=age, sex=sex, name=name,
        )
        result.errors = list(intake.errors)
        result.warnings = list(intake.warnings)
        result.goals = list(intake.goals)
        _progress("intake")

        if not intake.ok:
            result.ok = False
            result.blocked = True
            result.blocked_by = intake.blocked_by
            if intake.screen is not None and getattr(intake.screen, "blocked", False):
                result.advice = intake.screen.advice
            # 阻断也必须留痕（规格书 §4.1：记录触发依据）
            result.case_id = case_id or new_case_id()
            if self.store:
                self.store.add_audit(
                    result.case_id, "intake_blocked",
                    f"{result.blocked_by}: {'; '.join(result.errors)[:200]}",
                )
            return result

        # ---- 2. 脱敏预览（P3）：后续分析只用打码文本 ----
        payload = sanitized_payload(medical_history, main_issue)
        # ``masked_preview`` 是给前端展示的有限窗口；完整脱敏正文单独保存，
        # 后续画像、追问回答与就诊后更新都必须基于完整内容。
        result.masked_history = payload["medical_history"]
        result.masked_main_issue = payload["main_issue"]
        result.masked_preview = {
            "medical_history": payload["medical_history"][:2000],
            "main_issue": payload["main_issue"][:2000],
        }
        _progress("mask")

        # ---- 2.5 渐进式（progressive）：先只跑确定性规则，模型相位全部放后台 ----
        # 目的：让"点提交"到"看到自己的画像"从 3 秒进一步压到 0.2 秒级。
        # 规则画像是确定性抽取（无模型调用），已经能给患者核对；模型增强随后自动替换。
        if progressive:
            case_id = case_id or new_case_id()
            rule_extraction = extract(
                payload["medical_history"], case_id=case_id, main_issue=payload["main_issue"],
                use_llm=False, ai_service=None,
            )
            rule_profile = rule_extraction.profile
            if age not in (None, ""):
                try:
                    rule_profile.age = int(age)
                except (TypeError, ValueError):
                    pass
            if sex:
                rule_profile.sex = sex

            result.case_id = case_id
            result.gaps = rule_extraction.gaps
            result.completeness = rule_profile.completeness
            result.direct_quote_coverage = rule_profile.direct_quote_coverage
            result.passes_gate = rule_extraction.passes_gate
            result.profile = self._profile_to_dict(rule_profile)
            result.profile_is_rules_only = True
            result.warnings.extend(rule_extraction.warnings)
            _progress("profile")

            if not rule_extraction.passes_gate:
                result.ok = False
                result.case_state = STATE_NEED_MORE_INFO
                self._save_case(result, age=age, sex=sex, owner_id=owner_id)
                return result

            result.questions = [q.to_dict() for q in build_questions(gaps=result.gaps)]
            _progress("questions")

            # 规则分诊（S1 → S3，无模型发言）：先给一个可用的方向，模型到了再替换
            masked_text = payload["medical_history"]
            rule_eval = evaluate_rules(masked_text)
            outcome = orchestrate(text=masked_text, case_id=case_id, evaluation=rule_eval,
                                  model_picks=None, completeness=rule_profile.completeness,
                                  unresolved_gaps=len(result.gaps))
            result.triage = outcome.to_dict()
            _progress("triage")

            care_path = build_path(
                outcome=outcome, gaps=result.gaps,
                checks_by_department=self._checks_by_department(result.gaps, outcome.selected),
                case_id=case_id)
            result.path = care_path.to_dict()
            _progress("path")

            result.case_state = "analyzing"          # 状态机：AI 分析中（模型相位在后台跑）
            result.progressive = True
            result.analysis_pending = True
            result.ok = True
            _progress("mdt")
            self._save_case(result, age=age, sex=sex, owner_id=owner_id)
            self.start_analysis_job(case_id)         # profile → triage → mdt 全部后台
            _progress("save")
            return result

        # ---- 3. 病例结构化（P4：画像 + G4 门禁）----
        case_id = case_id or new_case_id()
        extraction: ExtractionResult = extract(
            payload["medical_history"], case_id=case_id, main_issue=payload["main_issue"],
            use_llm=self.use_llm, ai_service=self.ai_service,
        )
        profile = extraction.profile
        if age not in (None, ""):
            try:
                profile.age = int(age)
            except (TypeError, ValueError):
                pass
        if sex:
            profile.sex = sex

        result.case_id = case_id
        result.gaps = extraction.gaps
        result.completeness = profile.completeness
        result.direct_quote_coverage = profile.direct_quote_coverage
        result.passes_gate = extraction.passes_gate
        result.profile = self._profile_to_dict(profile)
        result.warnings.extend(extraction.warnings)
        _progress("profile")

        if not extraction.passes_gate:
            # 门禁未过：阻断进入分析，回画像确认补录（不再往下走）
            result.ok = False
            result.case_state = STATE_NEED_MORE_INFO
            self._save_case(result, age=age, sex=sex, owner_id=owner_id)
            return result

        # ---- 4. 智能追问（P5）----
        questions: List[Question] = build_questions(gaps=result.gaps)
        result.questions = [q.to_dict() for q in questions]
        _progress("questions")

        # ---- 5. 分诊决策链（P6）：S1 规则 → S2 受限发言 → S3 编排 ----
        masked_history = payload["medical_history"]
        rule_eval = evaluate_rules(masked_history)

        s2_result = None
        model_picks = None
        if self.use_llm and self.ai_service is not None:
            s2_result = s2_analyze(
                ai_service=self.ai_service, text=masked_history, evaluation=rule_eval,
                main_issue=payload["main_issue"], profile=result.profile,
            )
            result.s2 = s2_result.to_dict()
            model_picks = s2_result.orchestrator_picks
            if s2_result.method != "llm":
                result.warnings.append(f"S2 受限发言未生效（{s2_result.error or s2_result.method}）")

        outcome = orchestrate(text=masked_history, case_id=case_id,
                              evaluation=rule_eval, model_picks=model_picks,
                              completeness=profile.completeness,
                              unresolved_gaps=len(result.gaps))
        result.triage = outcome.to_dict()
        _progress("triage")

        # ---- 6. 就医路径与任务（P7）----
        checks_by_dept = self._checks_by_department(result.gaps, outcome.selected)
        care_path = build_path(outcome=outcome, gaps=result.gaps,
                               checks_by_department=checks_by_dept, case_id=case_id)
        result.path = care_path.to_dict()
        _progress("path")

        # ---- 7. 多专科独立研判 + 聚合（P8，耗时主要在这里）----
        deferred_mdt: Optional[Dict[str, Any]] = None
        _progress("mdt")
        if self.use_llm and self.ai_service is not None and outcome.selected:
            if defer_mdt:
                # 两段式：画像/追问/分诊/路径已就绪，MDT 等落库后由后台分析任务续跑
                deferred_mdt = {"phase_only": True}
                result.mdt_pending = True
                result.analysis_pending = True
            else:
                result.mdt = self._run_mdt(
                    text=masked_history, outcome=outcome, profile=result.profile,
                    case_id=case_id, rejections=[c.get("department") for c in outcome.conflicts],
                )

        result.case_state = STATE_AWAITING_DOCTOR

        if not outcome.selected:
            result.warnings.append("未能给出明确的就诊方向建议，请补充资料或线下咨询医生")

        result.ok = True
        self._save_case(result, age=age, sex=sex, owner_id=owner_id)
        if deferred_mdt:
            # 必须在落库之后启动：后台线程要能读到病例才能写进度
            self.start_analysis_job(case_id, phases=["mdt"])
        _progress("save")
        return result

    # ------------------------------------------------------------------ 追问回答
    @serialized_case
    def submit_answers(self, case_id: str, answers: Dict[str, str],
                       defer_mdt: bool = False) -> Dict[str, Any]:
        """提交追问回答：更新缺口状态 → **对受影响模块做增量重算**（规格书 §6.1）。

        - 只重跑分诊层（S1+S3）与路径，不重跑全流程；
        - 未回答/跳过的缺口如实保留，并降低可靠性（不编造）；
        - 全过程落审计。
        """
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        if not isinstance(answers, dict):
            return {"ok": False, "error": "answers 必须是 JSON 对象"}
        if len(answers) > MAX_ANSWER_ITEMS:
            return {"ok": False,
                    "error": f"answers 最多包含 {MAX_ANSWER_ITEMS} 项"}
        # 追问回答也会合并进病史并持久化，不能成为绕过 G3 的入口。
        safe_answers: Dict[str, str] = {}
        try:
            for key, value in answers.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    return {"ok": False, "error": "answers 的键和值必须是字符串"}
                if len(key) > MAX_ANSWER_KEY_LENGTH:
                    return {"ok": False,
                            "error": f"answers 的键长度不能超过 {MAX_ANSWER_KEY_LENGTH} 个字符"}
                safe_answers[key] = update_service.sanitize_update_content(
                    value, field_name="追问回答"
                ) if value.strip() else ""
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        payload = dict(case.get("payload") or {})
        gaps = list(payload.get("gaps", []))
        outcome_answers = apply_answers(gaps, safe_answers)

        # 已解决字段从缺口中移除
        remaining_gaps = list(outcome_answers["remaining_gaps"])

        # ---- 增量重算：把回答作为补充资料，只重跑分诊与路径 ----
        masked_history = str(payload.get("masked_history") or "")
        supplement = "；".join(
            f"{k}：{str(v).strip()}" for k, v in safe_answers.items()
            if str(v).strip() and str(v).strip() not in ("不清楚 / 跳过", "不清楚", "跳过")
        )
        recomputed_text = "；".join(x for x in (masked_history, supplement) if x)
        if len(recomputed_text) > update_service.MAX_MERGED_HISTORY_LENGTH:
            return {
                "ok": False,
                "error": (
                    f"合并后的病例正文过长（最多 {update_service.MAX_MERGED_HISTORY_LENGTH} 个字符），"
                    "请分批提交或先整理已有资料"
                ),
            }

        profile = payload.get("profile") or {}
        completeness = float(payload.get("completeness") or 0.0)
        rule_eval = evaluate_rules(recomputed_text)
        outcome = orchestrate(text=recomputed_text, case_id=case_id,
                              evaluation=rule_eval, model_picks=None,
                              completeness=completeness,
                              unresolved_gaps=len(outcome_answers["unresolved_fields"]))

        checks_by_dept = self._checks_by_department(remaining_gaps, outcome.selected)
        care_path = build_path(outcome=outcome, gaps=remaining_gaps,
                               checks_by_department=checks_by_dept, case_id=case_id)

        # ---- 增量更新原则：只有"建议科室集合发生变化"时才重跑多专科分析 ----
        old_selected = set((payload.get("triage") or {}).get("selected") or [])
        new_selected = set(outcome.selected)
        mdt = payload.get("mdt")
        deferred_mdt: Optional[Dict[str, Any]] = None
        if self.use_llm and self.ai_service is not None and new_selected != old_selected:
            if defer_mdt:
                # 科室集合变了要重跑 MDT —— 交给后台分析任务，不卡住"看分诊结果"
                mdt = None
                deferred_mdt = {"phase_only": True}
            else:
                mdt = self._run_mdt(
                    text=recomputed_text, outcome=outcome, profile=profile,
                    case_id=case_id,
                    answers={k: v for k, v in safe_answers.items() if str(v).strip()},
                    rejections=[c.get("department") for c in outcome.conflicts],
                )

        # ---- 落库 ----
        payload.update({
            "gaps": remaining_gaps,
            "answers": dict(safe_answers),
            "resolved_fields": outcome_answers["resolved_fields"],
            "unresolved_fields": outcome_answers["unresolved_fields"],
            "triage": outcome.to_dict(),
            "path": care_path.to_dict(),
            "tasks": care_path.to_dict().get("tasks", []),
            "mdt": mdt,
            "mdt_status": ({"state": "running", "done": 0,
                            "total": len(outcome.selected[:MAX_MDT_DEPARTMENTS]),
                            "current": "", "started_at": _now()} if deferred_mdt else payload.get("mdt_status")),
        })
        if self.store:
            self.store.save_case(
                case_id,
                patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "",
                status=case.get("status") or "running",
                case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
                report_version=int(case.get("report_version") or 1),
                payload=payload,
            )
            self.store.add_audit(
                case_id, "answers_submitted",
                f"已回答 {len(outcome_answers['resolved_fields'])} 项，"
                f"未解决 {len(outcome_answers['unresolved_fields'])} 项，"
                f"已重算分诊（建议科室 {len(outcome.selected)} 个）",
            )
        if deferred_mdt:
            # 必须在落库之后启动（后台线程要能读到病例）
            self.start_analysis_job(case_id, phases=["mdt"])

        return {
            "ok": True,
            "case_id": case_id,
            "remaining_gaps": remaining_gaps,
            "resolved_fields": outcome_answers["resolved_fields"],
            "unresolved_fields": outcome_answers["unresolved_fields"],
            "reliability_downgrade": bool(outcome_answers["unresolved_fields"]),
            "triage": outcome.to_dict(),      # 重算后的分诊结果（P6）
            "path": care_path.to_dict(),      # 重算后的路径与任务（P7）
            "mdt": mdt,                       # 多专科分析（P8，仅在科室变化时重跑）
            "mdt_pending": bool(deferred_mdt),
        }

    # ------------------------------------------------------------------ 后台分析任务
    # 设计：把"要模型参与的部分"从主请求里剥离，做成后台任务 + 阶段进度。
    #  - 三个相位：profile（画像增强）→ triage（S2 受限发言 + 重新编排）→ mdt（多学科意见卡 + 质询）
    #  - 进度落在 payload["analysis"]，`GET /api/v2/case/<id>/analysis` 可查，刷新页面也不丢
    #  - 单科病例（只有 1 个就医方向）跳过 MDT：不需要多学科，如实写"已跳过"而不是假装分析过
    #  - 失败不阻塞：前台保留已就绪的规则结果，如实写明失败原因，并提供重试

    ANALYSIS_PHASES = ("profile", "triage", "mdt")
    PHASE_LABELS = {
        "profile": "结构化病例画像（模型增强）",
        "triage": "就医方向（S2 受限发言 + 编排校验）",
        "mdt": "多学科独立意见 + 质询",
    }
    # 重跑某个相位前，先清掉该相位的**历史告警**：否则重跑成功了，"某科未完成"还挂在页面上
    PHASE_WARNING_HINTS = {
        "profile": ("结构化病例画像", "模型增强失败", "模型增强输出格式异常"),
        "triage": ("S2 受限发言", "就医方向（S2 受限发言"),
        "mdt": ("专科分析未完成", "多学科分析有", "意见卡", "仅 1 个就医方向",
                "未运行多学科会诊", "多学科独立意见", "无需多学科"),
    }

    @staticmethod
    def _new_analysis_status(*, mdt_total: int = 0) -> Dict[str, Any]:
        return {
            "state": "running",
            "phases": {phase: {"state": "pending", "label": CaseFlow.PHASE_LABELS[phase]}
                       for phase in CaseFlow.ANALYSIS_PHASES},
            "mdt": {"state": "pending", "done": 0, "total": mdt_total, "current": "", "step": "",
                    "failed": []},
            "warnings": [],
            "started_at": _now(),
            "finished_at": "",
        }

    def _patch_payload(self, case_id: str, **updates: Any) -> Dict[str, Any]:
        """原子更新病例 payload，避免后台分析与主请求相互覆盖。"""
        if not self.store:
            return {}
        def mutate(case: Dict[str, Any]) -> None:
            payload = dict(case.get("payload") or {})
            payload.update(updates)
            case["payload"] = payload
        case = self.store.mutate_case(case_id, mutate)
        return dict((case or {}).get("payload") or {})

    def _update_analysis(self, case_id: str, mutate: Callable[[Dict[str, Any]], None]) -> None:
        """对 payload["analysis"] 做增量修改后落库。"""
        if not self.store:
            return
        def mutate_case(case: Dict[str, Any]) -> None:
            payload = dict(case.get("payload") or {})
            status = dict(payload.get("analysis") or self._new_analysis_status())
            mutate(status)
            payload["analysis"] = status
            case["payload"] = payload
        self.store.mutate_case(case_id, mutate_case)

    def _set_phase(self, case_id: str, phase: str, state: str, *, note: str = "") -> None:
        def mutate(status: Dict[str, Any]) -> None:
            entry = status.setdefault("phases", {}).setdefault(phase, {})
            entry["state"] = state
            entry["label"] = self.PHASE_LABELS.get(phase, phase)
            if note:
                entry["note"] = note
            entry[f"{state}_at"] = _now()
        self._update_analysis(case_id, mutate)

    def _append_analysis_warning(self, case_id: str, text: str) -> None:
        if not text:
            return
        def mutate(status: Dict[str, Any]) -> None:
            warnings = status.setdefault("warnings", [])
            if text not in warnings:
                warnings.append(text)
        self._update_analysis(case_id, mutate)

    @serialized_case
    def start_analysis_job(self, case_id: str, *, phases: Optional[List[str]] = None) -> bool:
        """启动（或重试）后台分析任务。`phases` 指定只重跑哪些相位，默认全部。"""
        if not self.store:
            return False
        todo = list(phases or self.ANALYSIS_PHASES)
        if not todo or any(phase not in self.ANALYSIS_PHASES for phase in todo):
            return False
        todo = list(dict.fromkeys(todo))

        with self._jobs_lock:
            existing = self._active_jobs.get(case_id)
            if existing and existing.is_alive():
                return False
            case = self.store.load_case(case_id)
            if not case:
                return False
            payload = case.get("payload") or {}
            # 重置状态（重试时保留已完成的相位结果，只把待跑的相位置为 pending）
            status = dict(payload.get("analysis") or self._new_analysis_status())
            status["state"] = "running"
            status["finished_at"] = ""
            phases_map = dict(status.get("phases") or {})
            for phase in todo:
                phases_map[phase] = {"state": "pending", "label": self.PHASE_LABELS[phase]}
            status["phases"] = phases_map

            hints = tuple(hint for phase in todo
                          for hint in self.PHASE_WARNING_HINTS.get(phase, ()))
            if hints:
                status["warnings"] = [w for w in (status.get("warnings") or [])
                                      if not any(hint in str(w) for hint in hints)]
            if "mdt" in todo:
                status["mdt"] = {"state": "pending", "done": 0,
                                 "total": len(payload.get("triage", {}).get("selected", [])[:MAX_MDT_DEPARTMENTS]),
                                 "current": "", "step": "", "failed": []}
            payload["analysis"] = status
            self.store.transition_case(
                case_id, "analyzing", actor="system",
                reason=f"后台分析任务启动：{'、'.join(todo)}", payload=payload)
            self.store.add_audit(case_id, "analysis_started", f"后台分析任务启动：{'、'.join(todo)}")

            thread = threading.Thread(target=self._analysis_worker_guarded,
                                      args=(case_id, todo), daemon=True,
                                      name=f"analysis-{case_id}")
            self._active_jobs[case_id] = thread
            thread.start()
        return True

    def _analysis_worker_guarded(self, case_id: str, phases: List[str]) -> None:
        try:
            self._analysis_worker(case_id, phases)
        except Exception:  # noqa: BLE001 - 后台线程不能把异常打印成未处理堆栈
            # 相位内部已有“单相失败、保留其它结果”的处理；这里兜底的是线程
            # 启动后数据库被关闭/进程准备退出等更外层错误，避免 daemon 线程噪声
            # 和无意义的 traceback。服务重启后的 stale 任务仍可由重试接口恢复。
            logger.exception("后台分析任务异常退出：case_id=%s phases=%s", case_id, phases)
        finally:
            with self._jobs_lock:
                current = self._active_jobs.get(case_id)
                if current is threading.current_thread():
                    self._active_jobs.pop(case_id, None)

    @serialized_case
    def _analysis_worker(self, case_id: str, phases: List[str]) -> None:
        """后台执行分析相位；每个相位结束就落库，前台可随时取用（部分结果也有用）。"""
        case = self.store.load_case(case_id)
        if not case:
            return
        payload = dict(case.get("payload") or {})
        text = str(payload.get("masked_history") or "")
        main_issue = str(payload.get("masked_main_issue") or case.get("main_issue") or "")
        failed: List[str] = []

        def guard(phase: str, fn: Callable[[], None]) -> None:
            if phase not in phases:
                return
            self._set_phase(case_id, phase, "running")
            try:
                fn()
                self._set_phase(case_id, phase, "done")
            except Exception as exc:                        # noqa: BLE001 - 单相位失败不影响其他相位
                failed.append(phase)
                self._set_phase(case_id, phase, "failed",
                                note=f"{self.PHASE_LABELS.get(phase, phase)}阶段执行失败（可重试）")
                self._append_analysis_warning(
                    case_id, f"{self.PHASE_LABELS.get(phase, phase)}未完成（已保留其它已就绪结果）")

        # ---- 相位 1：画像增强（失败则保留规则画像）----
        def do_profile() -> None:
            refreshed = self.store.load_case(case_id)
            pl = dict((refreshed or {}).get("payload") or {})
            extraction = extract(text, case_id=case_id, main_issue=main_issue,
                                 use_llm=True, ai_service=self.ai_service)
            profile = extraction.profile
            rules = pl.get("profile_rules") or {}
            final = self._profile_to_dict(profile)
            self._patch_payload(case_id, profile=final, profile_rules=rules,
                                gaps=list(extraction.gaps),
                                completeness=profile.completeness,
                                direct_quote_coverage=profile.direct_quote_coverage,
                                questions=[q.to_dict() for q in build_questions(gaps=extraction.gaps)])
            for warning in extraction.warnings:
                self._append_analysis_warning(case_id, warning)

        # ---- 相位 2：S2 受限发言 + 重新编排 + 路径 ----
        def do_triage() -> None:
            refreshed = self.store.load_case(case_id)
            pl = dict((refreshed or {}).get("payload") or {})
            gaps = list(pl.get("gaps") or [])
            profile_dict = pl.get("profile") or {}
            evaluation = evaluate_rules(text)
            s2 = s2_analyze(ai_service=self.ai_service, text=text, evaluation=evaluation,
                            main_issue=main_issue, profile=profile_dict)
            if s2.method != "llm":
                self._append_analysis_warning(
                    case_id, f"S2 受限发言未生效（{s2.error or s2.method}），本次仅规则链路")
            outcome = orchestrate(text=text, case_id=case_id, evaluation=evaluation,
                                 model_picks=s2.orchestrator_picks,
                                 completeness=float(pl.get("completeness") or 0.0),
                                 unresolved_gaps=len(gaps))
            path = build_path(outcome=outcome, gaps=gaps,
                              checks_by_department=self._checks_by_department(gaps, outcome.selected),
                              case_id=case_id)
            self._patch_payload(case_id, s2=s2.to_dict(), triage=outcome.to_dict(),
                                path=path.to_dict(), tasks=path.to_dict().get("tasks", []))

        # ---- 相位 3：多学科独立意见 + 质询（单科跳过；逐科上报子步骤）----
        partial_mdt: Dict[str, Any] = {}          # 有科室没跑出来时，相位记为 partial（可重试）

        def do_mdt() -> None:
            refreshed = self.store.load_case(case_id)
            pl = dict((refreshed or {}).get("payload") or {})
            triage = pl.get("triage") or {}
            selected = list(triage.get("selected") or [])
            if len(selected) < 2:
                # 按需：只有一个就医方向 → 多学科会诊没有意义，如实标注"已跳过"
                # （A 型但方向 ≥2 的情况由 _run_mdt 按 behavior_type 判定，口径见下）
                def mutate(status: Dict[str, Any]) -> None:
                    status["mdt"] = {"state": "skipped", "done": 0, "total": 0, "current": "",
                                     "step": "仅 1 个就医方向，无需多学科会诊", "failed": []}
                self._update_analysis(case_id, mutate)
                self._patch_payload(case_id, mdt={"analyses": [], "aggregation": {
                    "consensus": [], "disagreements": [], "uncertainties": [],
                    "open_questions": [], "notes": ["仅 1 个就医方向，未运行多学科会诊"]}})
                return

            # MDT 必须使用上一相位已持久化的最终分诊结果。若在这里用
            # model_picks=None 重跑纯规则编排，S2 新增的科室会出现“页面推荐了，
            # 实际没参加 MDT”的不一致。
            outcome = SimpleNamespace(
                selected=selected,
                behavior_type=str(triage.get("behavior_type") or ""),
                conflicts=list(triage.get("conflicts") or []),
            )

            def on_step(dept: str, done: int, total: int, step: str) -> None:
                def mutate(status: Dict[str, Any]) -> None:
                    status["mdt"] = {"state": "running", "done": done, "total": total,
                                     "current": dept, "step": step}
                self._update_analysis(case_id, mutate)

            def on_department(dept: str, done: int, total: int) -> None:
                on_step(dept, done - 1, total, "正在读取资料并形成意见卡")

            mdt = self._run_mdt(text=text, outcome=outcome, profile=pl.get("profile"),
                                case_id=case_id, on_department=on_department)
            if isinstance(mdt, dict) and mdt.get("skipped_reason"):
                # A 型（单科为主）：不硬凑 MDT，相位如实记为 skipped + 写明原因
                reason = str(mdt["skipped_reason"])
                self._patch_payload(case_id, mdt=mdt)
                self._append_analysis_warning(case_id, reason)

                def mutate_skip(status: Dict[str, Any]) -> None:
                    status["mdt"] = {"state": "skipped", "done": 0, "total": len(selected),
                                     "current": "", "step": reason, "failed": []}
                self._update_analysis(case_id, mutate_skip)
                return
            if isinstance(mdt, dict):
                notes = list(((mdt.get("aggregation") or {}).get("notes")) or [])
                for note in notes:
                    if "未完成" in note or "跳过" in note or "修复" in note:
                        self._append_analysis_warning(case_id, note)
                for notice in (mdt.get("notices") or []):
                    self._append_analysis_warning(case_id, notice)
                failed_depts = list(mdt.get("failed_departments") or [])
                if failed_depts:
                    partial_mdt["failed"] = failed_depts
                    partial_mdt["cards"] = int(mdt.get("card_count") or 0)
            self._patch_payload(case_id, mdt=mdt)
            def mutate(status: Dict[str, Any]) -> None:
                failed_depts = list(partial_mdt.get("failed") or [])
                total = status["mdt"].get("total", 0)
                status["mdt"] = {"state": "done",
                                 "done": total - len(failed_depts) if failed_depts else total,
                                 "total": total, "current": "", "step": "",
                                 "failed": failed_depts}
            self._update_analysis(case_id, mutate)

        guard("profile", do_profile)
        guard("triage", do_triage)
        guard("mdt", do_mdt)

        # 逐科失败不算相位成功：只要有科室没跑出来，就把 mdt 记为 partial，
        # 这样"重新分析"才会重跑它（否则页面永远停在"某科未完成分析"且无从重试）
        mdt_failed = list(partial_mdt.get("failed") or [])
        if "mdt" in phases and mdt_failed and "mdt" not in failed:
            if partial_mdt.get("cards"):
                self._set_phase(case_id, "mdt", "partial",
                                note=f"{'、'.join(mdt_failed)} 未完成分析（可重试）")
            else:
                self._set_phase(case_id, "mdt", "failed",
                                note=f"{'、'.join(mdt_failed)} 均未完成分析（可重试）")
            self._append_analysis_warning(
                case_id, f"多学科分析有 {len(mdt_failed)} 个科室未完成（{'、'.join(mdt_failed)}），"
                         f"其余科室结果可用，可点「重新分析」重跑")

        # ---- 收尾：状态汇总 + 病例状态推进 ----
        if len(failed) == len(phases):
            state = "failed"
        elif failed or mdt_failed:
            state = "partial_failed"
        else:
            state = "done"

        def finish(status: Dict[str, Any]) -> None:
            status["state"] = state
            status["finished_at"] = _now()
        self._update_analysis(case_id, finish)

        refreshed = self.store.load_case(case_id)
        payload_now = dict((refreshed or {}).get("payload") or {})
        if payload_now.get("triage"):
            self.store.transition_case(
                case_id, STATE_AWAITING_DOCTOR, actor="system",
                reason="后台分析完成",
                payload=self._patch_payload(case_id, analysis=payload_now.get("analysis")))
        self.store.add_audit(case_id, "analysis_finished",
                             f"后台分析结束：{'、'.join(phases)}；失败相位={'、'.join(failed) or '无'}")

    def get_analysis(self, case_id: str) -> Optional[Dict[str, Any]]:
        """统一查询后台分析状态：三个相位 + MDT 细分进度 + 已就绪的结果（供前台增量渲染）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        payload = case.get("payload") or {}
        status = dict(payload.get("analysis") or {})
        if not status:
            # 老病例（同步分析，没有后台任务）：没有失败科室才按完成处理。
            failed = self._failed_departments(payload)
            state = self._effective_analysis_state(payload)
            status = {
                "state": state,
                "phases": {},
                "mdt": {"state": state if state in ("failed", "partial_failed") else "done",
                        "failed": failed},
                "warnings": ([f"历史病例仍有未完成专科分析：{'、'.join(failed)}"]
                              if failed else []),
            }
        else:
            # 兼容“相位 done 但 MDT 内含 unavailable”的修复前记录。
            effective_state = self._effective_analysis_state(payload)
            if effective_state != status.get("state"):
                status["state"] = effective_state
            failed = self._failed_departments(payload)
            if failed:
                mdt_status = dict(status.get("mdt") or {})
                mdt_status["failed"] = list(dict.fromkeys(
                    list(mdt_status.get("failed") or []) + failed
                ))
                status["mdt"] = mdt_status
        mdt = payload.get("mdt")
        return {
            "case_id": case_id,
            "case_state": case.get("case_state"),
            "analysis": status,
            "mdt_ready": bool(mdt),
            "profile": payload.get("profile"),
            "triage": payload.get("triage"),
            "path": payload.get("path"),
            "gaps": payload.get("gaps") or [],
            "questions": payload.get("questions") or [],
            "mdt": mdt,
            "warnings": list(status.get("warnings") or []),
        }

    @staticmethod
    def _failed_departments(payload: Dict[str, Any]) -> List[str]:
        """找出"没跑出意见卡的科室"。

        两个来源都要看：① 新数据写在 `analysis.mdt.failed`；
        ② **修复前就卡住的老病例**没有这个字段，只能从 `mdt.analyses` 里
        `status == "unavailable"` 的条目反推——否则这些病例永远无法重试。
        """
        analysis_mdt = ((payload.get("analysis") or {}).get("mdt") or {})
        out = [str(x) for x in (analysis_mdt.get("failed") or []) if str(x).strip()]
        for item in ((payload.get("mdt") or {}).get("analyses") or []):
            if isinstance(item, dict) and item.get("status") == "unavailable":
                dept = str(item.get("department") or "").strip()
                if dept and dept not in out:
                    out.append(dept)
        return out

    @classmethod
    def _effective_analysis_state(cls, payload: Dict[str, Any]) -> str:
        """从新旧两种 payload 形态推导可筛选的分析状态。

        老病例可能没有 ``analysis``，但 MDT 内的某个科室已经记录为
        ``unavailable``；即使新病例把相位总状态写成 ``done``，只要仍有失败科室，
        医生队列就必须显示为 ``partial_failed``/``failed``，否则既不会进入失败
        筛选，也不会被批处理重试。
        """
        payload = payload or {}
        status = payload.get("analysis") or {}
        state = str(status.get("state") or "")
        failed = cls._failed_departments(payload)
        if failed and state in ("", "done"):
            analyses = ((payload.get("mdt") or {}).get("analyses") or [])
            available = any(
                isinstance(item, dict) and item.get("status") not in (None, "unavailable")
                for item in analyses
            )
            return "partial_failed" if available else "failed"
        if state:
            return state
        return "done" if payload.get("mdt") else "none"

    @staticmethod
    def _is_stale_running(status: Dict[str, Any], minutes: int = 10) -> bool:
        """后台任务"很久没动静"（例如服务重启把线程丢了）→ 允许重跑，别永远卡住。"""
        if (status or {}).get("state") != "running":
            return False
        started = str((status or {}).get("started_at") or "")
        if not started:
            return False
        try:
            began = datetime.fromisoformat(started)
        except ValueError:
            return False
        return (datetime.now() - began).total_seconds() > minutes * 60

    def retry_analysis(self, case_id: str, phases: Optional[List[str]] = None) -> Dict[str, Any]:
        """重试失败/未完成的分析相位（默认只重跑没成功的那些）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        payload = case.get("payload") or {}
        status = payload.get("analysis") or {}
        if phases is not None:
            if not isinstance(phases, list) or any(
                    not isinstance(phase, str) or phase not in self.ANALYSIS_PHASES
                    for phase in phases):
                return {"ok": False,
                        "error": f"phases 只能包含：{'、'.join(self.ANALYSIS_PHASES)}"}
            phases = list(dict.fromkeys(phases))
        if not phases:
            if not status:
                # 老病例没有分析记录：按已有结果推断需要重跑哪些相位（别把已有的重算一遍）
                phases = ["mdt"] if payload.get("mdt") else list(self.ANALYSIS_PHASES)
            else:
                phases = [p for p in self.ANALYSIS_PHASES
                          if (status.get("phases", {}).get(p, {}) or {}).get("state") != "done"]
            # 相位标着完成、但里面有科室没跑出来 → 也算需要重试
            if "mdt" not in phases and self._failed_departments(payload):
                phases.append("mdt")
                phases.sort(key=self.ANALYSIS_PHASES.index)
        if not phases:
            return {"ok": False, "error": "所有分析相位都已完成，无需重试"}
        started = self.start_analysis_job(case_id, phases=phases)
        return {"ok": started, "retried": phases if started else [],
                "error": "" if started else "无法启动后台任务"}

    def analyze_pending_cases(self, limit: int = 3, reviewer_id: str = "") -> Dict[str, Any]:
        """医生端批处理：把"分析未完成（含个别科室未完成）"的病例排进后台任务。"""
        started: List[str] = []
        skipped: List[str] = []
        skipped_reasons: Dict[str, str] = {}
        if not self.store:
            return {"ok": False, "started": [], "skipped": [],
                    "skipped_reasons": {}, "error": "无存储"}

        safe_limit = max(1, min(int(limit), 100))
        # 不再只扫描最近 limit*4 条：授权病例可能很旧。按稳定的 created_at
        # 分页持续扫描，直到找到足够任务或读完整个授权集合。
        page_size = max(50, safe_limit * 4)
        offset = 0
        scanned = 0
        while len(started) < safe_limit:
            cases = (self.store.list_cases_for_reviewer(reviewer_id, limit=page_size, offset=offset)
                     if reviewer_id else self.store.list_cases(limit=page_size, offset=offset))
            if not cases:
                break
            scanned += len(cases)
            offset += len(cases)
            for case in cases:
                if len(started) >= safe_limit:
                    break
                case_id = str(case.get("case_id") or "")
                payload = case.get("payload") or {}
                if not payload.get("triage"):
                    skipped.append(case_id)
                    skipped_reasons[case_id] = "缺少规则分诊结果"
                    continue
                status = payload.get("analysis") or {}
                if status.get("state") == "running" and not self._is_stale_running(status):
                    skipped.append(case_id)
                    skipped_reasons[case_id] = "已有后台分析任务运行中"
                    continue
                if not status:
                    # 老病例（同步分析时代，没有分析记录）：已有完整 MDT 才跳过，
                    # 含 unavailable 的历史病例必须继续进入重试队列。
                    failed_legacy = self._failed_departments(payload)
                    if payload.get("mdt") and not failed_legacy:
                        skipped.append(case_id)
                        skipped_reasons[case_id] = "已有完整多学科分析"
                        continue
                    phases = ["mdt"] if payload.get("mdt") else list(self.ANALYSIS_PHASES)
                else:
                    phases = [p for p in self.ANALYSIS_PHASES
                              if (status.get("phases", {}).get(p, {}) or {}).get("state") != "done"]
                if "mdt" not in phases and self._failed_departments(payload):
                    phases.append("mdt")
                phases.sort(key=self.ANALYSIS_PHASES.index)
                if not phases:
                    skipped.append(case_id)
                    skipped_reasons[case_id] = "所有分析相位均已完成"
                    continue
                if self.start_analysis_job(case_id, phases=phases):
                    started.append(case_id)
                else:
                    skipped.append(case_id)
                    skipped_reasons[case_id] = "后台任务未能启动（可能已有并发任务）"
            if len(cases) < page_size:
                break
        return {"ok": True, "started": started, "skipped": skipped[:10],
                "skipped_reasons": {k: skipped_reasons[k] for k in skipped[:10] if k in skipped_reasons},
                "started_count": len(started), "scanned_count": scanned}

    # ------------------------------------------------------------------ 兼容入口（单看 MDT 的旧接口）
    def get_mdt_status(self, case_id: str) -> Optional[Dict[str, Any]]:
        """只查多学科分析：状态 + 进度 + 结果（保留给 P8 的轻量轮询）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        payload = case.get("payload") or {}
        mdt = payload.get("mdt")
        analysis = payload.get("analysis") or {}
        status = dict(analysis.get("mdt") or {})
        if not status:
            status = dict(payload.get("mdt_status") or {})
        failed = self._failed_departments(payload)
        if not status:
            effective_state = self._effective_analysis_state(payload)
            status = {
                "state": effective_state if effective_state in ("failed", "partial_failed")
                else ("done" if mdt else "none"),
                "done": 0,
                "total": 0,
                "current": "",
                "failed": failed,
            }
        elif failed:
            status["failed"] = list(dict.fromkeys(list(status.get("failed") or []) + failed))
            if status.get("state") == "done":
                status["state"] = "partial_failed" if mdt else "failed"
        return {"case_id": case_id, "status": status, "ready": bool(mdt), "mdt": mdt}

    # ------------------------------------------------------------------ 查询
    def list_patient_cases(self, owner_id: str, *, page: int = 1,
                           page_size: int = 10) -> Dict[str, Any]:
        """Build a patient-safe list of their cases without exposing raw payloads."""
        safe_page = max(1, int(page))
        safe_size = max(1, min(int(page_size), 50))
        total = self.store.count_cases_for_owner(owner_id) if self.store else 0
        cases = self.store.list_cases_for_owner(
            owner_id, limit=safe_size, offset=(safe_page - 1) * safe_size
        ) if self.store else []
        result = []
        for case in cases:
            payload = case.get("payload") or {}
            triage = payload.get("triage") or {}
            primary = (triage.get("primary") or {}).get("department") or ""
            routing = payload.get("routing") or {}
            requested = (routing.get("requested_department") or {}).get("name") or primary
            versions = self.store.list_report_versions(
                case.get("case_id") or "", include_reports=False) if self.store else []
            result.append({
                "case_id": case.get("case_id") or "",
                "created_at": case.get("created_at") or "",
                "updated_at": case.get("updated_at") or "",
                "main_issue": case.get("main_issue") or "",
                "case_state": case.get("case_state") or "",
                "case_state_label": case.get("case_state_label") or case.get("case_state") or "",
                "primary_department": requested,
                "routing_status": routing.get("status") or "not_submitted",
                "report_version": int(case.get("report_version") or 1),
                "published_versions": len(versions),
                "has_updates": bool(payload.get("updates")),
                "next_step": (payload.get("mdt_screening") or {}).get("patient_next_step") or "等待系统整理",
            })
        return {"cases": result, "page": safe_page, "page_size": safe_size,
                "total": total, "has_more": safe_page * safe_size < total}

    def get_patient_record(self, case_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        case = self.store.load_case(case_id) if self.store else None
        if not case or case.get("owner_id") != owner_id:
            return None
        payload = case.get("payload") or {}
        updates = []
        for item in payload.get("updates") or []:
            updates.append({key: item.get(key) for key in
                            ("update_id", "kind", "content", "source", "at")
                            if item.get(key) not in (None, "")})
        return {
            "case_id": case_id,
            "title": f"病例 #{case_id} · {case.get('main_issue') or '未填写主要问题'}",
            "main_issue": case.get("main_issue") or "",
            "submitted_material": {
                "medical_history": payload.get("initial_medical_history") or payload.get("masked_history") or "",
                "profile": payload.get("profile") or {},
                "submitted_at": case.get("created_at") or "",
            },
            "updates": updates,
            "report_version": int(case.get("report_version") or 1),
            "case_state": case.get("case_state") or "",
        }

    @serialized_case
    def submit_case_for_review(
        self,
        case_id: str,
        *,
        department_code: str,
        actor: str,
        reviewer_ids: Sequence[str],
    ) -> Dict[str, Any]:
        """患者确认主责科室后提交病例，并建立科室范围内的访问授权。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        raw_code = str(department_code or "").strip()
        requested_name = department_domain.to_name(raw_code)
        if requested_name is None:
            resolved = department_domain.resolve(raw_code)
            requested_name = resolved.name
        requested_code = department_domain.to_code(requested_name or "")
        if not requested_name or not requested_code:
            return {"ok": False, "error": "提交的科室不在支持的科室域内"}
        if not department_domain.can_be_primary(requested_name):
            return {"ok": False, "error": "辅助科室不能作为病例主责科室"}

        reviewers = list(dict.fromkeys(
            str(item).strip() for item in (reviewer_ids or []) if str(item).strip()
        ))
        payload = dict(case.get("payload") or {})
        existing = payload.get("routing") or {}
        existing_status = str(existing.get("status") or "")
        existing_requested = (existing.get("requested_department") or {}).get("code")
        if existing_status == "submitted":
            if existing_requested == requested_code:
                return {
                    "ok": True,
                    "changed": False,
                    "routing": existing,
                    "reviewer_count": len(reviewers),
                }
            return {"ok": False, "error": "病例已经提交到其他科室，不能直接改派"}

        if not reviewers:
            return {"ok": False, "error": "当前科室暂无可接收病例的医生"}

        triage = payload.get("triage") or {}
        primary = triage.get("primary") or {}
        triage_name = str(primary.get("department") or "").strip()
        triage_code = department_domain.to_code(triage_name) or ""
        now = _now()
        routing = {
            "status": "submitted",
            "triage_primary_department": {
                "code": triage_code,
                "name": triage_name,
            },
            "requested_department": {
                "code": requested_code,
                "name": requested_name,
            },
            "patient_adjusted": bool(triage_code and triage_code != requested_code),
            "submitted_at": now,
        }

        def mutate(current: Dict[str, Any]) -> None:
            next_payload = dict(current.get("payload") or {})
            next_payload["routing"] = routing
            current["payload"] = next_payload

        updated = self.store.mutate_case(case_id, mutate) if self.store else None
        if updated is None:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        for reviewer_id in reviewers:
            self.store.grant_case_access(
                case_id, reviewer_id, granted_by=actor, case_role="primary")
        self.store.add_audit(
            case_id,
            "case_submitted_for_review",
            f"department={requested_code}; reviewers={len(reviewers)}; patient_adjusted={routing['patient_adjusted']}",
        )
        return {
            "ok": True,
            "changed": True,
            "routing": routing,
            "reviewer_count": len(reviewers),
        }

    @serialized_case
    def submit_mdt_screening(
        self,
        case_id: str,
        *,
        decision: str,
        reason: str,
        purpose: str = "",
        departments: Optional[List[str]] = None,
        reviewer_id: str = "",
        department_reviewer_map: Optional[Mapping[str, Sequence[str]]] = None,
    ) -> Dict[str, Any]:
        """保存医生负责的 MDT 初筛决定（不改变病例主状态机）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        if str(case.get("case_state") or "") == "doctor_confirmed":
            return {"ok": False, "error": "已发布审核版的病例不能重新进行初筛"}

        decision = str(decision or "").strip()
        if decision not in MDT_SCREENING_DECISIONS:
            return {"ok": False, "error": "初筛决定非法"}
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            return {"ok": False, "error": "审核人不能为空"}

        try:
            safe_reason = update_service.sanitize_optional_content(reason, field_name="初筛原因")
            safe_purpose = update_service.sanitize_optional_content(purpose, field_name="会诊目的")
            safe_reviewer = update_service.sanitize_optional_content(
                reviewer_id, field_name="审核人")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not safe_reason:
            return {"ok": False, "error": "初筛原因不能为空"}
        if decision == "recommend_mdt" and not safe_purpose:
            return {"ok": False, "error": "建议 MDT 时会诊目的不能为空"}

        confirmation = (self.store.load_case(case_id).get("payload") or {}).get(
            "triage_doctor_confirmation") or {}
        departments_source = "mdt_request"
        if decision == "recommend_mdt" and confirmation.get("status") != "confirmed":
            return {"ok": False, "error": "请先完成分流确认，再启动正式 MDT"}
        if decision == "recommend_mdt" and not departments:
            raw_departments = list(confirmation.get("selected") or [])
            departments_source = "triage_confirmation"
        else:
            raw_departments = departments if departments is not None else []
        if not isinstance(raw_departments, list):
            return {"ok": False, "error": "科室必须是列表"}
        if len(raw_departments) > 6:
            return {"ok": False, "error": "参与科室最多 6 个"}
        normalized_departments: List[str] = []
        invalid_departments: List[str] = []
        for raw in raw_departments:
            if not isinstance(raw, str) or not raw.strip():
                invalid_departments.append(str(raw))
                continue
            resolved = department_domain.resolve(raw)
            if not resolved.name:
                invalid_departments.append(raw.strip())
                continue
            normalized_departments.append(resolved.name)
        if invalid_departments:
            return {"ok": False, "error": "科室不在支持的科室域内：" + "、".join(invalid_departments)}
        if len(normalized_departments) != len(set(normalized_departments)):
            return {"ok": False, "error": "参与科室必须是不同科室"}
        if decision == "recommend_mdt" and len(normalized_departments) < 2:
            return {"ok": False, "error": "建议 MDT 时至少两个不同科室"}

        if decision == "recommend_mdt":
            confirmed_departments = set(confirmation.get("selected") or [])
            if not set(normalized_departments).issubset(confirmed_departments):
                return {"ok": False, "error": "MDT 参与科室必须来自已确认的分流方向"}

        normalized_reviewer_map: Dict[str, List[str]] = {}
        if department_reviewer_map is not None:
            if not isinstance(department_reviewer_map, Mapping):
                return {"ok": False, "error": "科室协作者映射格式非法"}
            for raw_department, raw_reviewers in department_reviewer_map.items():
                if not isinstance(raw_reviewers, Sequence) or isinstance(
                    raw_reviewers, (str, bytes)
                ):
                    return {"ok": False, "error": "科室协作者列表格式非法"}
                department_name = department_domain.to_name(str(raw_department).strip())
                if department_name is None:
                    department_name = department_domain.resolve(str(raw_department)).name
                if not department_name:
                    continue
                normalized_reviewer_map[department_name] = list(dict.fromkeys(
                    str(item).strip() for item in raw_reviewers if str(item).strip()
                ))

        now = _now()
        screening = {
            "status": "decided",
            "decision": decision,
            "decision_label": MDT_SCREENING_LABELS[decision],
            "reason": safe_reason,
            "purpose": safe_purpose,
            "departments": normalized_departments,
            "departments_source": departments_source,
            "reviewer_id": safe_reviewer,
            "decided_at": now,
            "patient_next_step": MDT_SCREENING_NEXT_STEPS[decision],
            "mdt_queue_eligible": decision == "recommend_mdt",
        }
        payload = dict(case.get("payload") or {})
        payload["mdt_screening"] = screening
        self.store.save_case(
            case_id,
            owner_id=case.get("owner_id") or "",
            patient_name=case.get("patient_name") or "",
            main_issue=case.get("main_issue") or "",
            status=case.get("status") or "running",
            case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
            report_version=int(case.get("report_version") or 1),
            payload=payload,
        )
        self.store.add_audit(
            case_id,
            "mdt_screening_recorded",
            f"decision={decision}; departments={'、'.join(normalized_departments) or '无'}; reason=provided",
        )

        authorized_departments: List[str] = []
        pending_departments: List[str] = []
        authorized_reviewer_count = 0
        if decision == "recommend_mdt" and department_reviewer_map is not None:
            for department in normalized_departments:
                reviewers = normalized_reviewer_map.get(department, [])
                if not reviewers:
                    pending_departments.append(department)
                    continue
                authorized_departments.append(department)
                authorized_reviewer_count += len(reviewers)
                for reviewer in reviewers:
                    self.store.grant_case_access(
                        case_id, reviewer, granted_by=safe_reviewer, case_role="collaborator")
            if authorized_departments:
                self.store.add_audit(
                    case_id,
                    "mdt_access_granted",
                    f"departments={'、'.join(authorized_departments)}; reviewers={authorized_reviewer_count}",
                )
            if pending_departments:
                self.store.add_audit(
                    case_id,
                    "mdt_access_pending",
                    f"departments={'、'.join(pending_departments)}",
                )
        return {
            "ok": True,
            "mdt_screening": screening,
            "patient_mdt_screening": patient_safe_mdt_screening(screening),
            "authorized_departments": authorized_departments,
            "authorized_reviewer_count": authorized_reviewer_count,
            "pending_departments": pending_departments,
        }

    @serialized_case
    def submit_triage_confirmation(
        self, case_id: str, *, primary_department: str,
        secondary_departments: Optional[List[str]] = None, reason: str = "",
        reviewer_id: str = "",
    ) -> Dict[str, Any]:
        """保存医生对牵头/协同科室的确认，作为 MDT 和报告的前置决策。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        if str(case.get("case_state") or "") == "doctor_confirmed":
            return {"ok": False, "error": "已发布审核版的病例不能重新确认分流方向"}
        try:
            primary = department_domain.resolve(str(primary_department or "")).name
            safe_reason = update_service.sanitize_optional_content(reason, field_name="分流确认理由")
            safe_reviewer = update_service.sanitize_optional_content(reviewer_id, field_name="分流确认人")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not primary:
            return {"ok": False, "error": "主责科室不能为空"}
        if not safe_reason:
            return {"ok": False, "error": "分流确认理由不能为空"}
        raw_secondary = secondary_departments if secondary_departments is not None else []
        if not isinstance(raw_secondary, list) or len(raw_secondary) > 6:
            return {"ok": False, "error": "协同科室格式或数量不正确"}
        secondary: List[str] = []
        for raw in raw_secondary:
            name = department_domain.resolve(str(raw or "")).name
            if not name:
                return {"ok": False, "error": f"科室不在支持的科室域内：{raw}"}
            if name == primary or name in secondary:
                return {"ok": False, "error": "协同科室不能与主责科室重复"}
            secondary.append(name)
        if not safe_reviewer:
            return {"ok": False, "error": "分流确认人不能为空"}
        confirmation = {
            "status": "confirmed", "primary_department": primary,
            "secondary_departments": secondary, "selected": [primary] + secondary,
            "reason": safe_reason, "reviewer_id": safe_reviewer, "confirmed_at": _now(),
        }
        payload = dict(case.get("payload") or {})
        payload["triage_doctor_confirmation"] = confirmation
        self.store.save_case(case_id, owner_id=case.get("owner_id") or "",
                             patient_name=case.get("patient_name") or "",
                             main_issue=case.get("main_issue") or "",
                             status=case.get("status") or "running",
                             case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
                             report_version=int(case.get("report_version") or 1), payload=payload)
        self.store.add_audit(case_id, "triage_confirmation_recorded",
                             f"primary={primary}; secondary={len(secondary)}; reviewer={safe_reviewer}")
        return {"ok": True, "triage_doctor_confirmation": confirmation}

    @serialized_case
    def submit_mdt_formal_record(
        self, case_id: str, *, status: str, participants: Optional[List[str]] = None,
        conclusion: str = "", unresolved: str = "", next_step: str = "",
        reviewer_id: str = "",
    ) -> Dict[str, Any]:
        """保存真实 MDT 会诊记录，不把 AI 预分析当作医生结论。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        if str(case.get("case_state") or "") == "doctor_confirmed":
            return {"ok": False, "error": "已发布审核版的病例不能修改正式 MDT 记录"}
        payload = dict(case.get("payload") or {})
        screening = payload.get("mdt_screening") or {}
        if screening.get("decision") != "recommend_mdt":
            return {"ok": False, "error": "请先启动正式 MDT 后再记录会诊"}
        status = str(status or "").strip()
        if status not in MDT_FORMAL_RECORD_STATUSES:
            return {"ok": False, "error": "正式 MDT 状态非法"}
        if not isinstance(participants, list):
            return {"ok": False, "error": "参与人员必须是列表"}
        if len(participants) > 12:
            return {"ok": False, "error": "参与人员最多 12 人"}
        try:
            safe_reviewer = update_service.sanitize_optional_content(
                reviewer_id, field_name="正式 MDT 记录人")
            safe_participants = []
            for item in participants:
                value = update_service.sanitize_optional_content(
                    str(item or ""), field_name="参与人员")
                if value and value not in safe_participants:
                    safe_participants.append(value)
            safe_conclusion = update_service.sanitize_optional_content(
                conclusion, field_name="正式 MDT 结论")
            safe_unresolved = update_service.sanitize_optional_content(
                unresolved, field_name="正式 MDT 未解决事项")
            safe_next_step = update_service.sanitize_optional_content(
                next_step, field_name="正式 MDT 后续安排")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not safe_reviewer:
            return {"ok": False, "error": "正式 MDT 记录人不能为空"}
        if status == "completed" and not safe_conclusion:
            return {"ok": False, "error": "已完成的正式 MDT 必须填写结论"}

        record = {
            "status": status,
            "status_label": MDT_FORMAL_RECORD_STATUS_LABELS[status],
            "participants": safe_participants,
            "conclusion": safe_conclusion,
            "unresolved": safe_unresolved,
            "next_step": safe_next_step,
            "reviewer_id": safe_reviewer,
            "updated_at": _now(),
            "source": "doctor_recorded",
        }
        payload["mdt_formal_record"] = record
        self.store.save_case(
            case_id, owner_id=case.get("owner_id") or "",
            patient_name=case.get("patient_name") or "",
            main_issue=case.get("main_issue") or "",
            status=case.get("status") or "running",
            case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
            report_version=int(case.get("report_version") or 1), payload=payload)
        self.store.add_audit(
            case_id, "mdt_formal_record_updated",
            f"status={status}; participants={len(safe_participants)}; reviewer={safe_reviewer}",
        )
        return {"ok": True, "mdt_formal_record": record}

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        return self.store.load_case(case_id) if self.store else None

    def build_reports(self, case_id: str) -> Optional[Dict[str, Any]]:
        """生成患者版一页摘要 + 完整报告（P9）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        payload = dict(case.get("payload") or {})
        # 医生审核记录（若已有）用于"医生最终意见 / AI vs 医生差异"段
        payload["decisions"] = self.store.list_decisions(case_id) if self.store else []
        merged = dict(case)
        merged["payload"] = payload
        reports = report_service.build_both(merged)
        if self.store:
            self.store.add_audit(case_id, "report_generated",
                                 f"完整={reports['full'].get('complete')} "
                                 f"缺段={reports['full'].get('missing_sections')}")
        return reports

    # ------------------------------------------------------------------ 任务状态机（3.4）
    @serialized_case
    def update_task_state(self, case_id: str, task_id: str, to_state: str,
                          *, actor: str = "patient", reason: str = "") -> Dict[str, Any]:
        """变更任务状态（合法性由 task_service 校验）。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        payload = dict(case.get("payload") or {})
        path = dict(payload.get("path") or {})
        tasks = list(path.get("tasks") or [])
        try:
            safe_actor = update_service.sanitize_optional_content(
                actor, field_name="任务操作人") or "patient"
            safe_reason = update_service.sanitize_optional_content(
                reason, field_name="任务原因")
            update = task_service.update_state(tasks, task_id, to_state,
                                               actor=safe_actor, reason=safe_reason)
        except (task_service.TaskError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        path["tasks"] = tasks
        payload["path"] = path
        payload["tasks"] = tasks
        if self.store:
            self.store.save_case(
                case_id, patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "", status=case.get("status") or "running",
                case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
                report_version=int(case.get("report_version") or 1), payload=payload)
            self.store.add_audit(case_id, "task_updated",
                                 f"{task_id}: {update.from_state} → {update.to_state}"
                                 f"（{update.reason or '无原因'}）")
        return {"ok": True, "update": update.to_dict(),
                "tasks_summary": task_service.summarize(tasks)}

    # ------------------------------------------------------------------ 就诊后更新（3.6）
    @staticmethod
    def _card_from_analysis(data: Dict[str, Any]) -> Optional[SpecialistOpinionCard]:
        """把已保存的专科意见卡还原成聚合器可读取的结构。"""
        if not isinstance(data, dict) or data.get("status") not in (None, "ok"):
            return None
        department = str(data.get("department") or "").strip()
        if not department:
            return None
        inferences = []
        for raw in data.get("inferences") or []:
            if not isinstance(raw, dict) or not str(raw.get("text") or "").strip():
                continue
            confidence = str(raw.get("confidence") or "medium").lower()
            inferences.append(Inference(
                text=str(raw.get("text") or "").strip(),
                confidence=confidence if confidence in ("high", "medium", "low") else "medium",
                evidence_refs=[str(ref) for ref in raw.get("evidence_refs") or []],
            ))
        checks = []
        for raw in data.get("checks") or []:
            if not isinstance(raw, dict) or not str(raw.get("content") or "").strip():
                continue
            priority = str(raw.get("priority") or "medium").lower()
            checks.append(CheckItem(
                priority=priority if priority in ("high", "medium", "low") else "medium",
                content=str(raw.get("content") or "").strip(),
            ))
        return SpecialistOpinionCard(
            case_id="",
            specialty_code=department,
            specialty_name=department,
            focus=str(data.get("focus") or ""),
            facts=[str(value) for value in data.get("facts") or []],
            inferences=inferences,
            checks=checks,
            information_gaps=[str(value) for value in data.get("information_gaps") or []],
            referral=data.get("referral"),
        )

    def _refresh_affected_mdt(self, *, payload: Dict[str, Any], text: str,
                              departments: Sequence[str], case_id: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        """只刷新受影响的专科意见卡，保留其他科室的原始卡片。"""
        existing = payload.get("mdt") or {}
        if not existing or not departments:
            return existing or None, []
        analyses = [dict(item) for item in existing.get("analyses") or []
                     if isinstance(item, dict)]
        known_order = [str(item.get("department") or "") for item in analyses
                       if str(item.get("department") or "")]
        known = set(known_order)
        targets: List[str] = []
        for department in departments:
            department = str(department).strip()
            if not department or department in targets:
                continue
            # 保持 MDT 的既有最多 3 个科室上限；已有卡一定优先刷新，
            # 新入选科室只在还有分析名额时追加。
            if department not in known and len(known_order) + sum(
                    1 for target in targets if target not in known) >= MAX_MDT_DEPARTMENTS:
                continue
            targets.append(department)
        if not targets:
            return existing, []

        profile = payload.get("profile") or {}
        answers = payload.get("answers") or {}
        refreshed: Dict[str, Dict[str, Any]] = {}
        for department in targets:
            if self.use_llm and self.ai_service is not None:
                result = analyze_specialty(
                    ai_service=self.ai_service, department=department, text=text,
                    profile=profile, case_id=case_id, answers=answers,
                )
                refreshed[department] = result.to_dict()
            else:
                refreshed[department] = {
                    "department": department,
                    "status": "stale",
                    "method": "skipped",
                    "error": "新资料已进入，等待受影响科室重新分析",
                    "source_type": "ai_reference",
                    "source_label": "专科 AI 参考意见",
                }

        merged = []
        for item in analyses:
            department = str(item.get("department") or "")
            merged.append(refreshed.get(department, item))
        for department in targets:
            if department not in known:
                merged.append(refreshed[department])

        cards = [card for card in (self._card_from_analysis(item) for item in merged)
                 if card is not None]
        aggregation = aggregate(cards).to_dict() if cards else {
            "consensus": [], "disagreements": [], "uncertainties": [],
            "open_questions": [], "notes": ["受影响专科意见卡等待重新分析"],
        }
        notes = list(aggregation.get("notes") or [])
        if any(item.get("status") == "stale" for item in refreshed.values()):
            notes.append("部分受影响专科意见卡因模型不可用，等待重新分析")
        aggregation["notes"] = list(dict.fromkeys(notes))
        updated = dict(existing)
        updated["analyses"] = merged
        updated["aggregation"] = aggregation
        updated["departments"] = list(existing.get("departments") or known_order)
        for department in targets:
            if department not in updated["departments"]:
                updated["departments"].append(department)
        updated["affected_departments"] = targets
        return updated, targets

    @serialized_case
    def add_update(self, case_id: str, *, kind: str, content: str,
                   source: str = update_service.SOURCE_PATIENT,
                   department: str = "") -> Dict[str, Any]:
        """录入一条就诊后资料（患者上传 / 医生录入），并做**局部重算** + 变化摘要。

        患者上传的"医生意见"一律标记为待验证，不自动成为医生确认意见（主文档 §14）。
        """
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        try:
            update_service.validate_update(kind, content, source)
            safe_content = update_service.sanitize_update_content(content)
            safe_department = (
                update_service.sanitize_update_content(department, field_name="科室")
                if department else ""
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        payload = dict(case.get("payload") or {})
        old_triage = payload.get("triage") or {}
        old_path = payload.get("path") or {}
        current_departments = list(old_triage.get("selected") or [])
        old_review_items = review_service.build_items(case)

        # 依赖矩阵：确定受影响模块（只重算受影响部分）
        affected = update_service.determine_affected(kind, content, current_departments)

        update = update_service.UpdateRecord(
            update_id=f"U-{len(payload.get('updates') or []) + 1:03d}",
            case_id=case_id, kind=kind, content=safe_content, source=source,
            at=datetime.now().isoformat(timespec="seconds"),
            affected_departments=affected["departments"], affected_modules=affected["modules"],
        )

        # 医生意见：存入独立列表（不混入患者上传）。
        # 红线（主文档 §14）：患者转述的"医生说……"**不得**被当作医生确认意见，
        # 因此原样保留来源，由 build_doctor_opinion 判定 is_verified=False。
        if kind == "医生意见":
            opinions = list(payload.get("doctor_opinions") or [])
            opinions.append(update_service.build_doctor_opinion(
                safe_department or "线下医生", safe_content,
                source if source in update_service.VALID_SOURCES
                else update_service.SOURCE_UNVERIFIED))
            payload["doctor_opinions"] = opinions

        # ---- 新资料填补缺口：命中则从缺口列表移除（避免"已提供却仍显示缺失"）----
        filled_labels = update_service.gaps_filled_by(f"{kind}：{safe_content}")
        old_gaps = list(payload.get("gaps") or [])
        remaining_gaps = [g for g in old_gaps
                          if not any(label in str(g) for label in filled_labels)]
        filled_gaps = [g for g in old_gaps if g not in remaining_gaps]

        # ---- 局部重算：把新资料并入病历文本，只重跑分诊与路径 ----
        masked_history = str(payload.get("masked_history") or "")
        merged_text = "；".join(x for x in (masked_history, f"{kind}：{safe_content}") if x)
        if len(merged_text) > update_service.MAX_MERGED_HISTORY_LENGTH:
            return {
                "ok": False,
                "error": (
                    f"合并后的病例正文过长（最多 {update_service.MAX_MERGED_HISTORY_LENGTH} 个字符），"
                    "请分批提交或先整理已有资料"
                ),
            }
        rule_eval = evaluate_rules(merged_text)
        outcome = orchestrate(
            text=merged_text, case_id=case_id, evaluation=rule_eval, model_picks=None,
            completeness=float(payload.get("completeness") or 0.0),
            unresolved_gaps=len(remaining_gaps))
        checks_by_dept = self._checks_by_department(remaining_gaps, outcome.selected)
        care_path = build_path(outcome=outcome, gaps=remaining_gaps,
                               checks_by_department=checks_by_dept, case_id=case_id)

        new_triage = outcome.to_dict()
        new_path = care_path.to_dict()

        # 只重跑受影响的专科卡；更新后新出现的就医方向也需要首次生成意见卡。
        new_departments = list(new_triage.get("selected") or [])
        mdt_departments = list(dict.fromkeys(
            list(affected.get("departments") or []) +
            [department for department in new_departments if department not in current_departments]
        ))
        refreshed_mdt, refreshed_departments = self._refresh_affected_mdt(
            payload=dict(payload, triage=new_triage, path=new_path, gaps=remaining_gaps),
            text=merged_text, departments=mdt_departments, case_id=case_id,
        )

        # ---- 变化摘要 ----
        summary = update_service.build_change_summary(
            case_id=case_id, old_triage=old_triage, new_triage=new_triage,
            old_path=old_path, new_path=new_path, updates=[update],
            unresolved_gaps=remaining_gaps,
            doctor_opinions=payload.get("doctor_opinions") or [])
        for gap in filled_gaps:
            summary.added.append(f"已填补信息缺口：{gap}")
            if f"仍未确认：{gap}" in summary.needs_doctor_review:
                summary.needs_doctor_review.remove(f"仍未确认：{gap}")

        updates = list(payload.get("updates") or [])
        updates.append(update.to_dict())
        candidate_payload = dict(payload)
        candidate_payload.update({
            "updates": updates,
            "gaps": remaining_gaps,
            # 这是后续分析使用的完整脱敏正文，不能再截断成前端预览长度。
            "masked_history": merged_text,
            "triage": new_triage,
            "path": new_path,
            "tasks": new_path.get("tasks", []),
        })
        if refreshed_mdt is not None and refreshed_departments:
            candidate_payload["mdt"] = refreshed_mdt

        candidate_case = dict(case)
        candidate_case["payload"] = candidate_payload
        new_review_items = review_service.build_items(candidate_case)
        old_item_map = {item.item_id: item for item in old_review_items}
        new_item_map = {item.item_id: item for item in new_review_items}
        added_item_ids = sorted(set(new_item_map) - set(old_item_map))
        removed_item_ids = sorted(set(old_item_map) - set(new_item_map))
        changed_item_ids = review_service.affected_item_ids(
            old_review_items, new_review_items, affected.get("departments") or [],
            f"{kind}：{safe_content}")
        reopened_item_ids = sorted(
            item_id for item_id in changed_item_ids
            if item_id in old_item_map and item_id in new_item_map
        )
        item_labels = {item.item_id: item.title for item in new_review_items}
        affected_item_labels = [item_labels[item_id] for item_id in changed_item_ids
                                if item_id in item_labels]
        reopened_item_labels = [item_labels[item_id] for item_id in reopened_item_ids
                                if item_id in item_labels]
        revisions = dict(payload.get("review_item_revisions") or {})
        for item_id in set(old_item_map) | set(new_item_map):
            try:
                revisions[item_id] = max(1, int(revisions.get(item_id, 1) or 1))
            except (TypeError, ValueError):
                revisions[item_id] = 1
        for item_id in reopened_item_ids:
            revisions[item_id] = revisions.get(item_id, 1) + 1
        candidate_payload["review_item_revisions"] = revisions

        unaffected_departments = [
            department for department in current_departments
            if department not in (affected.get("departments") or [])
        ]
        recomputed_phases = ["triage", "path"] if affected.get("departments") else []
        if refreshed_departments:
            recomputed_phases.append("mdt")
        review_item_changes = {
            "affected_item_ids": changed_item_ids,
            "affected_item_labels": affected_item_labels,
            "reopened_item_ids": reopened_item_ids,
            "reopened_item_labels": reopened_item_labels,
            "added_item_ids": added_item_ids,
            "removed_item_ids": removed_item_ids,
            "affected_departments": list(affected.get("departments") or []),
            "unaffected_departments": unaffected_departments,
            "recomputed_phases": recomputed_phases,
        }
        summary_payload = summary.to_dict()
        summary_payload.update(review_item_changes)
        candidate_payload["review_item_changes"] = review_item_changes
        candidate_payload["change_summary"] = summary_payload
        payload = candidate_payload
        affected = dict(affected)
        affected.update(review_item_changes)
        # 新资料进入后待复核（状态机）
        if self.store:
            current_state = str(case.get("case_state") or STATE_AWAITING_DOCTOR)
            target_state = "awaiting_recheck" if current_state == "doctor_confirmed" \
                else STATE_AWAITING_DOCTOR
            self.store.transition_case(
                case_id, target_state, actor=source, reason=f"{kind}更新", payload=payload)
            self.store.add_audit(case_id, "case_updated",
                                 f"{kind}（{source}）→ 受影响科室：{affected['departments'] or '无'}"
                                 f"；变化摘要 {len(summary.changed)} 项")

        change_payload = dict(summary_payload)
        change_payload["report_versions"] = self.store.list_report_versions(
            case_id, include_reports=False) if self.store else []
        return {"ok": True, "update": update.to_dict(),
                "change_summary": change_payload,
                "triage": new_triage, "path": new_path,
                "affected": affected}

    def get_change_summary(self, case_id: str) -> Optional[Dict[str, Any]]:
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        payload = case.get("payload") or {}
        return {
            "change_summary": payload.get("change_summary"),
            "updates": payload.get("updates") or [],
            "doctor_opinions": payload.get("doctor_opinions") or [],
            # 患者端的版本变化只读取已保存的发布快照；旧病例没有快照时保持空列表，
            # 不根据当前可变病例内容伪造历史版本。
            "report_versions": self.store.list_report_versions(
                case_id, include_reports=False) if self.store else [],
        }

    def get_report_versions(self, case_id: str, *, include_reports: bool = True) -> Optional[Dict[str, Any]]:
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        return {
            "case_id": case_id,
            "current_version": int(case.get("report_version") or 1),
            "versions": self.store.list_report_versions(
                case_id, include_reports=include_reports) if self.store else [],
        }

    # ------------------------------------------------------------------ 医生端审核台（4.1-4.5）
    QUEUE_FILTERS = (
        ("doctor_pending", "待我处理"),
        ("conflict", "有冲突"),
        ("gaps", "缺关键证据"),
        ("analysis_failed", "分析失败/部分"),
        ("low_reliability", "可靠性低"),
        ("recheck", "待复核"),
        ("exempt", "含免审条目"),
    )
    QUEUE_SORTS = (
        ("risk", "风险优先"),
        ("pending", "待审最多"),
        ("updated", "最近更新"),
    )

    @staticmethod
    def _queue_risk(*, pending: int, conflict_count: int, gap_count: int,
                    reliability: str, analysis_state: str) -> Tuple[str, int]:
        """按可解释信号计算病例队列风险，不使用模型置信度作唯一依据。"""
        if (conflict_count or gap_count or reliability == "低"
                or analysis_state in ("failed", "partial_failed")):
            return "高", 3
        if pending or reliability == "中":
            return "中", 2
        return "低", 1

    def list_review_cases(
        self,
        limit: Optional[int] = 20,
        state_filter: str = "",
        *,
        offset: int = 0,
        filter_kind: str = "",
        sort: str = "risk",
        department: str = "",
        reviewer_id: str = "",
    ) -> Dict[str, Any]:
        """屏 A 病例队列：先过滤/排序，再返回服务端分页窗口。

        审核摘要是动态计算的，因此先从 SQLite 读取病例并计算队列字段，
        随后在完整候选集上应用筛选与排序，最后才切出当前页，避免把前 100
        条病例误当成完整队列。
        """
        if not self.store:
            return {"rows": [], "total": 0, "has_more": False,
                    "filters": {}, "sorts": list(self.QUEUE_SORTS),
                    "review_summary": {"required": 0, "sample": 0, "automatic": 0,
                                       "high_risk": 0, "conflict": 0, "analysis_failed": 0,
                                       "reopened_items": 0, "stale_decisions": 0}}

        rows: List[Dict[str, Any]] = []
        # 授权筛选尽量下推到 SQLite，避免审核员每次打开队列都读取全库病例。
        if reviewer_id:
            cases = self.store.list_cases_for_reviewer(reviewer_id)
        else:
            cases = self.store.list_cases(limit=None)
        decision_map = self.store.list_decisions_for_cases(
            [str(case.get("case_id") or "") for case in cases]
        )
        for case in cases:
            payload = case.get("payload") or {}
            screening = payload.get("mdt_screening") or {}
            # 急症处理已经离开普通 MDT 流程，不进入医生常规审核队列。
            if screening.get("decision") == "urgent_care":
                continue
            triage = payload.get("triage") or {}
            raw_items = review_service.build_items(case)
            case_decisions = decision_map.get(str(case.get("case_id") or ""), [])
            stale = review_service.stale_decisions(raw_items, case_decisions)
            items = review_service.apply_decisions(raw_items, case_decisions)
            summary = review_service.summarize(items)
            item_changes = payload.get("review_item_changes") or {}
            summary["stale_decisions"] = len(stale)
            summary["reopened_items"] = len(item_changes.get("reopened_item_ids") or [])
            primary = triage.get("primary") or {}
            profile = payload.get("profile") or {}
            analysis_state = self._effective_analysis_state(payload)
            aggregation = (payload.get("mdt") or {}).get("aggregation") or {}
            conflict_count = len(aggregation.get("disagreements") or [])
            conflict_count += len(triage.get("conflicts") or [])
            disagreement_map = review_service.latest_disagreement_decisions(
                payload.get("disagreement_decisions") or [])
            pending_disagreements = sum(
                1 for index, _ in enumerate(aggregation.get("disagreements") or [], start=1)
                if f"X-{index:02d}" not in disagreement_map
                or disagreement_map[f"X-{index:02d}"].get("status")
                not in review_service.DISAGREEMENT_STATUSES
            )
            summary["disagreements"] = len(aggregation.get("disagreements") or [])
            summary["disagreements_pending"] = pending_disagreements
            gap_count = len(payload.get("gaps") or [])
            reliability = str((triage.get("reliability") or {}).get("level") or "")
            risk_level, risk_rank = self._queue_risk(
                pending=int(summary.get("pending") or 0),
                conflict_count=conflict_count,
                gap_count=gap_count,
                reliability=reliability,
                analysis_state=analysis_state,
            )
            rows.append({
                "case_id": case.get("case_id"),
                "case_state": case.get("case_state"),
                "case_state_label": CASE_STATE_LABELS.get(str(case.get("case_state")), case.get("case_state")),
                "patient_name": case.get("patient_name") or "（脱敏病例）",
                "age": profile.get("age"),
                "sex": profile.get("sex"),
                "behavior_type": triage.get("behavior_type"),
                "reliability": reliability,
                "primary_department": primary.get("department"),
                "chief_complaint": profile.get("chief_complaint") or case.get("main_issue") or "",
                "pending_items": int(summary.get("pending") or 0),
                "exempt_items": int(summary.get("auto_processed") or 0),
                "required_items": int(summary.get("required") or 0),
                "sample_items": int(summary.get("sample") or 0),
                "automatic_items": int(summary.get("automatic") or 0),
                "high_risk_items": int(summary.get("high_risk") or 0),
                "conflict_items": int(summary.get("conflict") or 0),
                "analysis_failed_items": int(summary.get("analysis_failed") or 0),
                "reopened_item_ids": list(item_changes.get("reopened_item_ids") or []),
                "reopened_item_labels": list(item_changes.get("reopened_item_labels") or []),
                "stale_decisions": len(stale),
                "pending_disagreements": pending_disagreements,
                "conflict_count": conflict_count,
                "gap_count": gap_count,
                "risk_level": risk_level,
                "risk_rank": risk_rank,
                "review": summary,
                "reviewed_by": (payload.get("review") or {}).get("published_by") or "",
                "mdt_screening": {
                    "decision": screening.get("decision") or "",
                    "decision_label": screening.get("decision_label") or "",
                    "mdt_queue_eligible": screening.get("mdt_queue_eligible"),
                } if screening else None,
                "report_version": int(case.get("report_version") or 1),
                "analysis_state": analysis_state,
                "analysis_phases": {k: (v or {}).get("state")
                                    for k, v in ((payload.get("analysis") or {}).get("phases") or {}).items()},
                "created_at": case.get("created_at"),
                "updated_at": case.get("updated_at"),
            })

        if state_filter:
            rows = [r for r in rows if r["case_state"] == state_filter]
        if department:
            rows = [r for r in rows if r.get("primary_department") == department]
        review_summary = {
            "required": sum(int(r.get("required_items") or 0) for r in rows),
            "sample": sum(int(r.get("sample_items") or 0) for r in rows),
            "automatic": sum(int(r.get("automatic_items") or 0) for r in rows),
            "high_risk": sum(int(r.get("high_risk_items") or 0) for r in rows),
            "conflict": sum(int(r.get("conflict_items") or 0) for r in rows),
            "analysis_failed": sum(int(r.get("analysis_failed_items") or 0) for r in rows),
            "reopened_items": sum(len(r.get("reopened_item_ids") or []) for r in rows),
            "stale_decisions": sum(int(r.get("stale_decisions") or 0) for r in rows),
            "disagreements": sum(int((r.get("review") or {}).get("disagreements") or 0)
                                 for r in rows),
            "disagreements_pending": sum(int(r.get("pending_disagreements") or 0) for r in rows),
        }
        # 筛选计数使用当前状态/科室范围，但不受当前 filter_kind 影响，
        # 这样左轨可以告诉医生切换筛选后预计有多少病例，而不会把计数显示成误导性的 0。
        filter_counts = {
            "all": len(rows),
            "doctor_pending": sum(1 for r in rows
                                   if r["pending_items"] > 0 or r.get("pending_disagreements", 0) > 0),
            "conflict": sum(1 for r in rows if r["conflict_count"] > 0),
            "gaps": sum(1 for r in rows if r["gap_count"] > 0),
            "analysis_failed": sum(1 for r in rows if r["analysis_state"] in ("failed", "partial_failed")),
            "low_reliability": sum(1 for r in rows if r["reliability"] == "低"),
            "recheck": sum(1 for r in rows if r["case_state"] == "awaiting_recheck"),
            "exempt": sum(1 for r in rows if r["exempt_items"] > 0),
        }
        if filter_kind == "doctor_pending":
            rows = [r for r in rows
                    if r["pending_items"] > 0 or r.get("pending_disagreements", 0) > 0]
        elif filter_kind == "conflict":
            rows = [r for r in rows if r["conflict_count"] > 0]
        elif filter_kind == "gaps":
            rows = [r for r in rows if r["gap_count"] > 0]
        elif filter_kind == "analysis_failed":
            rows = [r for r in rows if r["analysis_state"] in ("failed", "partial_failed")]
        elif filter_kind == "low_reliability":
            rows = [r for r in rows if r["reliability"] == "低"]
        elif filter_kind == "recheck":
            rows = [r for r in rows if r["case_state"] == "awaiting_recheck"]
        elif filter_kind == "exempt":
            rows = [r for r in rows if r["exempt_items"] > 0]

        if sort == "pending":
            rows.sort(key=lambda r: (
                -int(r["pending_items"] or 0), -int(r["risk_rank"] or 0),
                str(r.get("updated_at") or r.get("created_at") or ""), str(r.get("case_id") or ""),
            ))
        elif sort == "updated":
            rows.sort(key=lambda r: (
                str(r.get("updated_at") or r.get("created_at") or ""),
                str(r.get("case_id") or ""),
            ), reverse=True)
        else:
            rows.sort(key=lambda r: (
                -int(r["risk_rank"] or 0), -int(r["pending_items"] or 0),
                -int(r["conflict_count"] or 0),
                str(r.get("updated_at") or r.get("created_at") or ""), str(r.get("case_id") or ""),
            ))

        safe_offset = max(0, int(offset))
        safe_limit = None if limit is None else max(1, min(int(limit), 100))
        total = len(rows)
        page = rows[safe_offset:] if safe_limit is None else rows[safe_offset:safe_offset + safe_limit]
        return {
            "rows": page,
            "total": total,
            "has_more": bool(safe_limit is not None and safe_offset + safe_limit < total),
            "filters": filter_counts,
            "sorts": list(self.QUEUE_SORTS),
            "review_summary": review_summary,
        }

    def get_review(self, case_id: str) -> Optional[Dict[str, Any]]:
        """屏 B-F 数据：决策条目 + 审核进度 + 是否可发布 + 线下意见。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        decisions = self.store.list_decisions(case_id) if self.store else []
        raw_items = review_service.build_items(case)
        stale = review_service.stale_decisions(raw_items, decisions)
        items = review_service.apply_decisions(raw_items, decisions)
        summary = review_service.summarize(items)
        payload = case.get("payload") or {}
        item_changes = payload.get("review_item_changes") or {}
        summary["stale_decisions"] = len(stale)
        summary["reopened_items"] = len(item_changes.get("reopened_item_ids") or [])
        disagreement_decisions = review_service.latest_disagreement_decisions(
            payload.get("disagreement_decisions") or [])
        disagreements = report_service.disagreements_for_report(
            ((payload.get("mdt") or {}).get("aggregation") or {}).get("disagreements") or [],
            payload.get("disagreement_decisions") or [])
        pending_disagreements = [
            item.get("分歧编号") for item in disagreements
            if item.get("分歧编号") not in disagreement_decisions
            or disagreement_decisions[item.get("分歧编号")].get("status")
            not in review_service.DISAGREEMENT_STATUSES
        ]
        summary["disagreements"] = len(disagreements)
        summary["disagreements_pending"] = len(pending_disagreements)
        summary["disagreements_resolved"] = sum(
            1 for record in disagreement_decisions.values()
            if record.get("status") == "resolved")
        summary["disagreements_unresolved"] = sum(
            1 for record in disagreement_decisions.values()
            if record.get("status") in ("unresolved_with_followup", "need_more_info"))
        return {
            "case_id": case_id,
            "case_state": case.get("case_state"),
            "case_state_label": CASE_STATE_LABELS.get(str(case.get("case_state")), case.get("case_state")),
            "mdt_screening": payload.get("mdt_screening") or None,
            "triage_doctor_confirmation": payload.get("triage_doctor_confirmation") or None,
            "mdt_formal_record": payload.get("mdt_formal_record") or None,
            "mdt_preparation_packet": report_service.build_mdt_preparation_packet(case),
            "items": [i.to_dict() for i in items],
            "summary": summary,
            "pending_questions": review_service.pending_questions(items),
            "decisions": decisions,
            "review": payload.get("review") or {},
            "doctor_opinions": payload.get("doctor_opinions") or [],
            "change_summary": payload.get("change_summary"),
            "stale_decisions": stale,
            "review_item_changes": payload.get("review_item_changes") or {},
            "disagreements": disagreements,
            "disagreement_decisions": list(disagreement_decisions.values()),
            "pending_disagreements": pending_disagreements,
            "published": bool((payload.get("review") or {}).get("published")),
        }

    @serialized_case
    def submit_decision(self, case_id: str, item_id: str, action: str, *,
                        after: str = "", reason: str = "",
                        reviewer_name: str = "模拟审核员", reviewer_dept: str = "") -> Dict[str, Any]:
        """逐条审核（屏 F）：接受 / 修改 / 驳回 / 暂不确定 / 请求补资料。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        # 审核后的改写内容、理由会进入 decisions 表和审计时间线，同样不能
        # 允许通过审核接口把手机号/邮箱等原文写回持久化层。
        try:
            safe_after = update_service.sanitize_optional_content(
                after, field_name="审核修改内容")
            safe_reason = update_service.sanitize_optional_content(
                reason, field_name="审核理由")
            safe_reviewer = update_service.sanitize_optional_content(
                reviewer_name, field_name="审核人") or "模拟审核员"
            safe_reviewer_dept = update_service.sanitize_optional_content(
                reviewer_dept, field_name="审核科室")
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "action": action}

        items = review_service.build_items(case)
        try:
            item = review_service.find_item(items, item_id)
            decisions = self.store.list_decisions(case_id) if self.store else []
            decision = review_service.make_decision(
                case_id=case_id, item=item, action=action, after=safe_after, reason=safe_reason,
                reviewer_name=safe_reviewer, reviewer_dept=safe_reviewer_dept,
                version=int(case.get("report_version") or 1),
                content_revision=item.content_revision,
                decision_id=f"{case_id}-{item.target_type}-{item.target_id}-{len(decisions) + 1}",
            )
        except review_service.ReviewError as exc:
            return {"ok": False, "error": str(exc), "action": action}

        if self.store:
            record = dict(decision.__dict__)
            # reviewer 必须以普通 dict 落库（payload 走 json.dumps，不能塞 dataclass）
            record["reviewer"] = {"role": safe_reviewer or "模拟审核员", "dept": safe_reviewer_dept}
            record["reviewer_name"] = safe_reviewer
            self.store.save_decision(record)
            self.store.add_audit(
                case_id, "decision_recorded",
                f"{review_service.ACTION_LABELS.get(action, action)} {item.title}"
                + (f"｜原因：{safe_reason}" if safe_reason else "")
                + (f"｜改为：{safe_after}" if action == "modify" and safe_after else ""),
            )

        # 请求补资料 → 病例退回补资料（状态机 + 留痕）
        state_changed = None
        if action == "request_more_info" and self.store:
            from_state = str(case.get("case_state") or STATE_AWAITING_DOCTOR)
            self.store.transition_case(
                case_id, STATE_NEED_MORE_INFO, actor=safe_reviewer or "doctor",
                reason=f"请求补资料：{item.title}", status="running")
            self.store.add_audit(case_id, "case_state_changed",
                                 f"{from_state} → {STATE_NEED_MORE_INFO}（请求补资料）")
            state_changed = STATE_NEED_MORE_INFO

        review = self.get_review(case_id) or {}
        return {"ok": True, "decision": decision.__dict__, "state_changed": state_changed,
                "items": review.get("items"), "summary": review.get("summary"),
                "pending_questions": review.get("pending_questions") or []}

    @serialized_case
    def submit_disagreement_decision(
        self, case_id: str, disagreement_id: str, status: str, *, choice: str = "",
        resolution: str = "", reason: str = "", next_step: str = "",
        reviewer_name: str = "模拟审核员", reviewer_dept: str = ""
    ) -> Dict[str, Any]:
        """记录牵头医生对某条 MDT 分歧的独立处理决定。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        payload = dict(case.get("payload") or {})
        disagreements = (((payload.get("mdt") or {}).get("aggregation") or {})
                         .get("disagreements") or [])
        safe_id = str(disagreement_id or "").strip().upper()
        if safe_id.isdigit():
            safe_id = f"X-{int(safe_id):02d}"
        index = None
        for i, entry in enumerate(disagreements, start=1):
            if safe_id == f"X-{i:02d}":
                index = i
                topic = str(entry.get("topic") or "")
                break
        if index is None:
            return {"ok": False, "error": f"分歧不存在：{disagreement_id}"}

        try:
            safe_choice = update_service.sanitize_optional_content(
                choice or resolution, field_name="牵头医生决定")
            safe_reason = update_service.sanitize_optional_content(
                reason, field_name="分歧处理理由")
            safe_next_step = update_service.sanitize_optional_content(
                next_step, field_name="下一步处理方式")
            safe_reviewer = update_service.sanitize_optional_content(
                reviewer_name, field_name="责任审核人") or "模拟审核员"
            safe_reviewer_dept = update_service.sanitize_optional_content(
                reviewer_dept, field_name="审核科室")
            record = review_service.make_disagreement_decision(
                disagreement_id=f"X-{index:02d}", topic=topic, status=status,
                choice=safe_choice, reason=safe_reason, next_step=safe_next_step,
                reviewer_name=safe_reviewer, reviewer_dept=safe_reviewer_dept,
            )
        except (ValueError, review_service.ReviewError) as exc:
            return {"ok": False, "error": str(exc)}

        records = list(payload.get("disagreement_decisions") or [])
        records.append(record)
        payload["disagreement_decisions"] = records
        if self.store:
            self.store.save_case(
                case_id, owner_id=case.get("owner_id") or "",
                patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "",
                status=case.get("status") or "running",
                case_state=case.get("case_state") or STATE_AWAITING_DOCTOR,
                report_version=int(case.get("report_version") or 1), payload=payload)
            self.store.add_audit(
                case_id, "disagreement_decision_recorded",
                f"{record['disagreement_id']}={record['status']}；责任审核人={safe_reviewer}")
        review = self.get_review(case_id) or {}
        return {
            "ok": True,
            "disagreement": record,
            "disagreements": review.get("disagreements") or [],
            "pending_disagreements": review.get("pending_disagreements") or [],
        }

    @serialized_case
    def publish_review(self, case_id: str, *, reviewer_name: str = "模拟审核员",
                       accept_all: bool = False) -> Dict[str, Any]:
        """发布审核版报告（屏 F 底部）：状态 → doctor_confirmed、版本 +1、角标转绿。

        规则（写死）：
        - 仍有"**必审**条目未处理"时不得发布；`accept_all=True` 时把这些条目**逐条**批量接受并留痕
          （不是静默通过：每条都落一条决策记录）；
        - **规则判定免审的条目不计入待审、也不阻塞发布**（修复前它们会，实测占条目 21.9%），
          且**不得**被批量记成"医生接受"；
        - 批量接受遇到高风险、冲突、分析失败、科室选择或关键路径项目时直接停用，必须逐条处理；
        - 发布后病例状态 `doctor_confirmed`、`report_version + 1`、审计留痕。
        """
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}
        try:
            safe_reviewer = update_service.sanitize_optional_content(
                reviewer_name, field_name="审核人") or "模拟审核员"
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        items = review_service.apply_decisions(
            review_service.build_items(case),
            self.store.list_decisions(case_id) if self.store else [])
        # 待审 = 必审且未处理（auto_processed 既不计入待审，也不能被批量接受写成医生确认）
        pending = [i for i in items
                   if i.needs_doctor_review and i.state == review_service.AWAITING]

        accepted_ids: List[str] = []
        if pending and accept_all:
            blocked = [item for item in pending if not item.batch_eligible]
            if blocked:
                return {
                    "ok": False,
                    "error": "高风险、冲突、分析失败、科室选择和关键路径条目必须逐条处理，不能批量接受",
                    "pending_items": [item.item_id for item in pending],
                    "batch_blocked_items": [
                        {"item_id": item.item_id, "reason": item.batch_block_reason}
                        for item in blocked
                    ],
                }
            for item in pending:
                result = self.submit_decision(
                    case_id, item.item_id, "accept",
                    reason="批量接受必审条目（发布时统一确认）", reviewer_name=safe_reviewer)
                if result.get("ok"):
                    accepted_ids.append(item.item_id)
            items = review_service.apply_decisions(
                review_service.build_items(case),
                self.store.list_decisions(case_id) if self.store else [])
            pending = [i for i in items
                       if i.needs_doctor_review and i.state == review_service.AWAITING]

        if pending:
            return {"ok": False,
                    "error": f"仍有 {len(pending)} 条必审条目未处理，不能发布审核版",
                    "pending_items": [i.item_id for i in pending]}

        summary = review_service.summarize(items)
        if not summary["total"]:
            return {"ok": False, "error": "该病例没有可审核条目，无法发布审核版"}
        if not summary["can_publish"]:
            return {"ok": False,
                    "error": "；".join(summary.get("blocking_reasons") or ["暂不能发布审核版"]),
                    "summary": summary}

        payload = dict(case.get("payload") or {})
        screening = payload.get("mdt_screening") or {}
        if screening.get("status") == "decided" and (payload.get("triage") or {}).get("primary") \
                and (payload.get("triage_doctor_confirmation") or {}).get("status") != "confirmed":
            return {"ok": False, "error": "请先在分流确认中确认主责和协同科室"}
        formal_record = payload.get("mdt_formal_record") or {}
        if (screening.get("decision") == "recommend_mdt"
                and formal_record.get("status") != "completed"):
            return {"ok": False, "error": "正式 MDT 尚未完成记录，不能发布审核版"}
        disagreements = (((payload.get("mdt") or {}).get("aggregation") or {})
                         .get("disagreements") or [])
        disagreement_records = list(payload.get("disagreement_decisions") or [])
        disagreement_map = review_service.latest_disagreement_decisions(disagreement_records)
        pending_disagreements = [
            f"X-{index:02d}" for index, _ in enumerate(disagreements, start=1)
            if f"X-{index:02d}" not in disagreement_map
            or disagreement_map[f"X-{index:02d}"].get("status")
            not in review_service.DISAGREEMENT_STATUSES
        ]
        summary["disagreements"] = len(disagreements)
        summary["disagreements_pending"] = len(pending_disagreements)
        summary["disagreements_resolved"] = sum(
            1 for record in disagreement_map.values() if record.get("status") == "resolved")
        summary["disagreements_unresolved"] = sum(
            1 for record in disagreement_map.values()
            if record.get("status") in ("unresolved_with_followup", "need_more_info"))
        if pending_disagreements:
            return {
                "ok": False,
                "error": f"仍有 {len(pending_disagreements)} 条分歧未记录明确处理状态，不能发布审核版",
                "pending_disagreements": pending_disagreements,
                "summary": summary,
            }

        new_version = int(case.get("report_version") or 1) + 1
        published_at = _now()
        payload["review"] = {
            "published": True,
            "published_by": safe_reviewer,
            "published_at": published_at,
            "version": new_version,
            "counts": summary["by_state"],
            "human_touched": summary["human_touched"],
            "doctor_reviewed": summary["doctor_reviewed"],
            # 免审条目如实记进报告与审计，且**不写成医生确认**
            "auto_processed": summary["auto_processed"],
            "batch_accepted": accepted_ids,
        }
        # "暂不确定"条目进入报告的待医生确认问题清单
        payload["review_pending_questions"] = review_service.pending_questions(items)
        # 报告生成使用当前审核决定，但决定本身继续保存在 decisions 表中。
        snapshot_payload = dict(payload)
        snapshot_payload["decisions"] = self.store.list_decisions(case_id) if self.store else []
        snapshot_case = dict(case)
        snapshot_case["case_state"] = "doctor_confirmed"
        snapshot_case["report_version"] = new_version
        snapshot_case["payload"] = snapshot_payload
        reports = report_service.build_both(snapshot_case)
        snapshot = {
            "case_id": case_id,
            "version": new_version,
            "published_by": safe_reviewer,
            "published_at": published_at,
            "review_summary": summary,
            "report_json": {"summary": reports["summary"], "full": reports["full"]},
            "summary_markdown": reports["summary_markdown"],
            "full_markdown": reports["full_markdown"],
        }
        if self.store:
            self.store.transition_case(
                case_id, "doctor_confirmed", actor=safe_reviewer or "doctor",
                reason="发布审核版报告", status="reviewed",
                report_version=new_version, payload=payload,
                report_version_snapshot=snapshot)
            self.store.add_audit(
                case_id, "report_published",
                f"审核人={safe_reviewer} 版本={new_version} 医生处理={summary['doctor_reviewed']}/"
                f"{summary['total']} 免审自动处理={summary['auto_processed']} "
                f"批量接受={len(accepted_ids)} 分歧={len(disagreements)}")
        return {"ok": True, "case_state": "doctor_confirmed", "report_version": new_version,
                "summary": summary, "batch_accepted": accepted_ids,
                "review": payload["review"], "snapshot": {
                    "version": new_version, "published_by": safe_reviewer,
                    "published_at": published_at,
                }}

    def get_review_history(self, case_id: str) -> Optional[Dict[str, Any]]:
        """屏 G 审核记录与版本：审计时间线 + 版本 + 条目状态快照。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        decisions = self.store.list_decisions(case_id) if self.store else []
        raw_items = review_service.build_items(case)
        current_revisions = {item.item_id: item.content_revision for item in raw_items}
        items = review_service.apply_decisions(raw_items, decisions)
        payload = case.get("payload") or {}
        timeline: List[Dict[str, Any]] = []
        for row in self.store.list_audit(case_id, limit=200) if self.store else []:
            timeline.append({"at": row.get("at"), "event": row.get("event"),
                             "detail": row.get("detail")})
        return {
            "case_id": case_id,
            "report_version": int(case.get("report_version") or 1),
            "review": payload.get("review") or {},
            "decisions": [
                {"decision_id": d.get("decision_id"), "action": d.get("action"),
                 "action_label": review_service.ACTION_LABELS.get(str(d.get("action")), d.get("action")),
                 "target_type": d.get("target_type"),
                 "target_label": review_service.TARGET_TYPE_LABELS.get(
                     str(d.get("target_type")), d.get("target_type")),
                 "target_id": d.get("target_id"),
                 "before": d.get("before"), "after": d.get("after"),
                 "reason": d.get("reason"),
                 "reviewer": (d.get("reviewer") or {}).get("role"),
                 "created_at": d.get("created_at"), "version": d.get("version"),
                 "content_revision": review_service._decision_revision(d),
                 "current_content_revision": current_revisions.get(
                     f"{d.get('target_type')}:{d.get('target_id')}"),
                 "stale": f"{d.get('target_type')}:{d.get('target_id')}" not in current_revisions
                 or review_service._decision_revision(d) != current_revisions.get(
                     f"{d.get('target_type')}:{d.get('target_id')}"),}
                for d in decisions
            ],
            "items": [i.to_dict() for i in items],
            "versions": self.store.list_report_versions(case_id) if self.store else [],
            "timeline": timeline,
        }

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _checks_by_department(gaps: Sequence[str], selected: Sequence[str]) -> Dict[str, List[str]]:
        """把"信息缺口"映射到"该向哪个科室确认"（用依赖矩阵反查，而不是字符串包含）。

        修正：缺口文案是"眼底检查：信息不足"（字段名），并不含科室名，
        原来用 `dep in gap` 判断永远匹配不上，导致没有任何"就医咨询"任务。
        """
        from services.dependency_matrix import BY_LABEL, departments_for_field

        out: Dict[str, List[str]] = {}
        for gap in gaps or []:
            field = None
            for label, key in BY_LABEL.items():
                if label in str(gap):
                    field = key
                    break
            if not field:
                continue
            item = f"完善{str(gap).split('：')[0]}"
            for dep in departments_for_field(field):
                if dep in (selected or []) and item not in out.setdefault(dep, []):
                    out[dep].append(item)
        return out

    def _profile_to_dict(self, profile: Any) -> Dict[str, Any]:
        """画像 → 前端可渲染的结构（保留 source_refs 供 hover 出处）。"""
        return {
            "case_id": profile.case_id,
            "age": profile.age,
            "sex": profile.sex,
            "chief_complaint": profile.chief_complaint,
            "diseases": [
                {"name": d.name, "duration": d.duration, "confidence": d.confidence,
                 "source_refs": list(d.source_refs)} for d in profile.diseases
            ],
            "symptoms_timeline": [
                {"symptom": s.symptom, "onset": s.onset, "confidence": s.confidence,
                 "source_refs": list(s.source_refs)} for s in profile.symptoms_timeline
            ],
            "exams": [
                {"item": e.item, "value": e.value, "abnormal": e.abnormal,
                 "source_refs": list(e.source_refs)} for e in profile.exams
            ],
            "medications": [{"name": m.name, "dose": m.dose} for m in profile.medications],
            "information_gaps": list(profile.information_gaps),
        }

    def _save_case(self, result: CaseCreation, *, age: Any = None, sex: str = "",
                   owner_id: str = "") -> None:
        if not self.store:
            return
        self.store.save_case(
            result.case_id,
            owner_id=owner_id,
            patient_name="",
            main_issue=(result.masked_preview.get("main_issue") or "")[:100],
            status="running" if result.ok else "blocked",
            case_state=result.case_state,
            payload={
                "goals": list(result.goals),
                # 不要使用 masked_preview：它是前端展示窗口，最多 2000 字。
                "masked_history": result.masked_history or result.masked_preview.get("medical_history", ""),
                # 后续补充会合并到 masked_history，保留首次提交内容供患者回看。
                "initial_medical_history": result.masked_history or result.masked_preview.get("medical_history", ""),
                "masked_main_issue": result.masked_main_issue or result.masked_preview.get("main_issue", ""),
                "gaps": list(result.gaps),
                "completeness": result.completeness,
                "direct_quote_coverage": result.direct_quote_coverage,
                "questions": result.questions,
                "profile": result.profile,
                "profile_rules": result.profile if result.profile_is_rules_only else {},
                "analysis": (self._new_analysis_status(
                    mdt_total=len(((result.triage or {}).get("selected") or [])[:MAX_MDT_DEPARTMENTS])
                ) if (result.progressive or result.mdt_pending) else {}),
                "triage": result.triage,
                "path": result.path,
                "tasks": (result.path or {}).get("tasks", []),
            },
        )
        self.store.add_audit(
            result.case_id, "case_created",
            f"状态={result.case_state} 完整度={result.completeness} "
            f"直引覆盖={result.direct_quote_coverage} 追问={len(result.questions)} "
            f"建议科室={len((result.triage or {}).get('selected', []))}",
        )
        self._record_transition_path(result.case_id, STATE_PENDING, result.case_state)

    # 合法状态转移链路（须与 services/case_state.py 的 TRANSITIONS 一致）
    _TRANSITION_PATHS = {
        "analyzing": ["analyzing"],
        "awaiting_doctor": ["analyzing", "awaiting_doctor"],
        "need_more_info": ["need_more_info"],
        "pending_materials": [],
    }

    def _record_transition_path(self, case_id: str, from_state: str, to_state: str) -> None:
        """按状态机的合法链路逐跳记录（避免出现非法转移记录）。"""
        if not self.store or to_state == from_state:
            return
        hops = self._TRANSITION_PATHS.get(to_state)
        if hops is None:
            hops = [to_state]
        current = from_state
        for nxt in hops:
            self.store.save_transition(case_id, current, nxt, actor="system",
                                       reason="病例流程推进")
            current = nxt

    def _audit(self, case_id: Optional[str], event: str, detail: str) -> None:
        if self.store and case_id:
            self.store.add_audit(case_id, event, detail)
