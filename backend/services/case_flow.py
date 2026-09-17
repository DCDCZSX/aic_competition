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
from functools import wraps
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ask_service import Question, apply_answers, build_questions  # noqa: E402
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
from services.rule_engine import evaluate as evaluate_rules  # noqa: E402
from services.s2_analyst import analyze as s2_analyze  # noqa: E402
from services.store import Store  # noqa: E402
from services.triage import orchestrate  # noqa: E402
from schemas import CASE_STATE_LABELS  # noqa: E402

# 耗时控制：多专科分析最多取前 3 个科室
MAX_MDT_DEPARTMENTS = 3

# 病例状态（与 schemas.CASE_STATES 保持一致）
STATE_PENDING = "pending_materials"
STATE_AWAITING_DOCTOR = "awaiting_doctor"
STATE_NEED_MORE_INFO = "need_more_info"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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
        result.masked_preview = {
            "medical_history": payload["medical_history"][:2000],
            "main_issue": payload["main_issue"],
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

        payload = dict(case.get("payload") or {})
        gaps = list(payload.get("gaps", []))
        outcome_answers = apply_answers(gaps, answers or {})

        # 已解决字段从缺口中移除
        remaining_gaps = list(outcome_answers["remaining_gaps"])

        # ---- 增量重算：把回答作为补充资料，只重跑分诊与路径 ----
        masked_history = str(payload.get("masked_history") or "")
        supplement = "；".join(
            f"{k}：{str(v).strip()}" for k, v in (answers or {}).items()
            if str(v).strip() and str(v).strip() not in ("不清楚 / 跳过", "不清楚", "跳过")
        )
        recomputed_text = "；".join(x for x in (masked_history, supplement) if x)

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
                    answers={k: v for k, v in (answers or {}).items() if str(v).strip()},
                    rejections=[c.get("department") for c in outcome.conflicts],
                )

        # ---- 落库 ----
        payload.update({
            "gaps": remaining_gaps,
            "answers": dict(answers or {}),
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
        if not todo:
            return False

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
        main_issue = str(case.get("main_issue") or "")
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
                                note=f"{type(exc).__name__}: {exc}"[:180])
                self._append_analysis_warning(
                    case_id, f"{self.PHASE_LABELS.get(phase, phase)}未完成："
                             f"{type(exc).__name__}（已保留其它已就绪结果）")

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
            # 老病例（同步分析，没有后台任务）：按"已完成"处理，避免前端一直转圈
            status = {"state": "done", "phases": {}, "mdt": {"state": "done", "failed": []},
                      "warnings": []}
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
        if not self.store:
            return {"ok": False, "started": [], "skipped": [], "error": "无存储"}
        allowed = set(self.store.list_authorized_case_ids(reviewer_id)) if reviewer_id else set()
        for case in self.store.list_cases(limit=limit * 4):
            if len(started) >= limit:
                break
            case_id = case.get("case_id") or ""
            if reviewer_id and case_id not in allowed:
                continue
            payload = case.get("payload") or {}
            if not payload.get("triage"):
                skipped.append(case_id)          # 连规则分诊都没有，不具备分析条件
                continue
            status = payload.get("analysis") or {}
            if status.get("state") == "running" and not self._is_stale_running(status):
                skipped.append(case_id)          # 正在跑，避免重复排任务
                continue
            if not status:
                # 老病例（同步分析时代，没有分析记录）：
                # 已有 MDT 结果且没有失败科室 → 视为已完成；否则按需重跑
                failed_legacy = self._failed_departments(payload)
                if payload.get("mdt") and not failed_legacy:
                    skipped.append(case_id)
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
                continue
            if self.start_analysis_job(case_id, phases=phases):
                started.append(case_id)
        return {"ok": True, "started": started, "skipped": skipped[:10],
                "started_count": len(started)}

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
            status = (dict(payload.get("mdt_status") or {})
                      or {"state": "done" if mdt else "none", "done": 0, "total": 0, "current": ""})
        return {"case_id": case_id, "status": status, "ready": bool(mdt), "mdt": mdt}

    # ------------------------------------------------------------------ 查询
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
            update = task_service.update_state(tasks, task_id, to_state,
                                               actor=actor, reason=reason)
        except task_service.TaskError as exc:
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
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        payload = dict(case.get("payload") or {})
        old_triage = payload.get("triage") or {}
        old_path = payload.get("path") or {}
        current_departments = list(old_triage.get("selected") or [])

        # 依赖矩阵：确定受影响模块（只重算受影响部分）
        affected = update_service.determine_affected(kind, content, current_departments)

        update = update_service.UpdateRecord(
            update_id=f"U-{len(payload.get('updates') or []) + 1:03d}",
            case_id=case_id, kind=kind, content=str(content).strip(), source=source,
            at=datetime.now().isoformat(timespec="seconds"),
            affected_departments=affected["departments"], affected_modules=affected["modules"],
        )

        # 医生意见：存入独立列表（不混入患者上传）。
        # 红线（主文档 §14）：患者转述的"医生说……"**不得**被当作医生确认意见，
        # 因此原样保留来源，由 build_doctor_opinion 判定 is_verified=False。
        if kind == "医生意见":
            opinions = list(payload.get("doctor_opinions") or [])
            opinions.append(update_service.build_doctor_opinion(
                department or "线下医生", str(content).strip(),
                source if source in update_service.VALID_SOURCES
                else update_service.SOURCE_UNVERIFIED))
            payload["doctor_opinions"] = opinions

        # ---- 新资料填补缺口：命中则从缺口列表移除（避免"已提供却仍显示缺失"）----
        filled_labels = update_service.gaps_filled_by(f"{kind}：{content}")
        old_gaps = list(payload.get("gaps") or [])
        remaining_gaps = [g for g in old_gaps
                          if not any(label in str(g) for label in filled_labels)]
        filled_gaps = [g for g in old_gaps if g not in remaining_gaps]

        # ---- 局部重算：把新资料并入病历文本，只重跑分诊与路径 ----
        masked_history = str(payload.get("masked_history") or "")
        merged_text = "；".join(x for x in (masked_history, f"{kind}：{content}") if x)
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
        payload.update({
            "updates": updates,
            "gaps": remaining_gaps,
            "masked_history": merged_text[:2000],
            "triage": new_triage,
            "path": new_path,
            "tasks": new_path.get("tasks", []),
            "change_summary": summary.to_dict(),
        })
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

        return {"ok": True, "update": update.to_dict(),
                "change_summary": summary.to_dict(),
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
        }

    # ------------------------------------------------------------------ 医生端审核台（4.1-4.5）
    def list_review_cases(self, limit: int = 100, state_filter: str = "",
                          reviewer_id: str = "") -> List[Dict[str, Any]]:
        """屏 A 病例列表：按"待审优先"排序，带待审条数与关键摘要。"""
        if not self.store:
            return []
        rows: List[Dict[str, Any]] = []
        allowed = set(self.store.list_authorized_case_ids(reviewer_id)) if reviewer_id else set()
        for case in self.store.list_cases(limit=limit):
            if reviewer_id and case.get("case_id") not in allowed:
                continue
            payload = case.get("payload") or {}
            triage = payload.get("triage") or {}
            items = review_service.apply_decisions(
                review_service.build_items(case),
                self.store.list_decisions(case.get("case_id") or ""),
            )
            summary = review_service.summarize(items)
            primary = triage.get("primary") or {}
            rows.append({
                "case_id": case.get("case_id"),
                "case_state": case.get("case_state"),
                "case_state_label": CASE_STATE_LABELS.get(str(case.get("case_state")), case.get("case_state")),
                "patient_name": case.get("patient_name") or "（脱敏病例）",
                "age": (payload.get("profile") or {}).get("age"),
                "sex": (payload.get("profile") or {}).get("sex"),
                "behavior_type": triage.get("behavior_type"),
                "reliability": (triage.get("reliability") or {}).get("level"),
                "primary_department": primary.get("department"),
                "chief_complaint": (payload.get("profile") or {}).get("chief_complaint")
                                   or case.get("main_issue") or "",
                "pending_items": summary["pending"],
                "review": summary,
                "reviewed_by": (payload.get("review") or {}).get("published_by") or "",
                "report_version": int(case.get("report_version") or 1),
                "analysis_state": (payload.get("analysis") or {}).get("state") or ("done" if payload.get("mdt") else "none"),
                "analysis_phases": {k: (v or {}).get("state")
                                    for k, v in ((payload.get("analysis") or {}).get("phases") or {}).items()},
                "created_at": case.get("created_at"),
                "updated_at": case.get("updated_at"),
            })
        if state_filter:
            rows = [r for r in rows if r["case_state"] == state_filter]
        # 待审优先 → 新病例优先
        rows.sort(key=lambda r: (-int(r["pending_items"] or 0), str(r["created_at"] or "")), reverse=False)
        rows.sort(key=lambda r: (-int(r["pending_items"] or 0),))
        return rows

    def get_review(self, case_id: str) -> Optional[Dict[str, Any]]:
        """屏 B-F 数据：决策条目 + 审核进度 + 是否可发布 + 线下意见。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        decisions = self.store.list_decisions(case_id) if self.store else []
        items = review_service.apply_decisions(review_service.build_items(case), decisions)
        summary = review_service.summarize(items)
        payload = case.get("payload") or {}
        return {
            "case_id": case_id,
            "case_state": case.get("case_state"),
            "case_state_label": CASE_STATE_LABELS.get(str(case.get("case_state")), case.get("case_state")),
            "items": [i.to_dict() for i in items],
            "summary": summary,
            "pending_questions": review_service.pending_questions(items),
            "decisions": decisions,
            "review": payload.get("review") or {},
            "doctor_opinions": payload.get("doctor_opinions") or [],
            "change_summary": payload.get("change_summary"),
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

        items = review_service.build_items(case)
        try:
            item = review_service.find_item(items, item_id)
            decisions = self.store.list_decisions(case_id) if self.store else []
            decision = review_service.make_decision(
                case_id=case_id, item=item, action=action, after=after, reason=reason,
                reviewer_name=reviewer_name, reviewer_dept=reviewer_dept,
                version=int(case.get("report_version") or 1),
                decision_id=f"{case_id}-{item.target_type}-{item.target_id}-{len(decisions) + 1}",
            )
        except review_service.ReviewError as exc:
            return {"ok": False, "error": str(exc), "action": action}

        if self.store:
            record = dict(decision.__dict__)
            # reviewer 必须以普通 dict 落库（payload 走 json.dumps，不能塞 dataclass）
            record["reviewer"] = {"role": reviewer_name or "模拟审核员", "dept": reviewer_dept}
            record["reviewer_name"] = reviewer_name
            self.store.save_decision(record)
            self.store.add_audit(
                case_id, "decision_recorded",
                f"{review_service.ACTION_LABELS.get(action, action)} {item.title}"
                + (f"｜原因：{reason}" if reason else "")
                + (f"｜改为：{after}" if action == "modify" and after else ""),
            )

        # 请求补资料 → 病例退回补资料（状态机 + 留痕）
        state_changed = None
        if action == "request_more_info" and self.store:
            from_state = str(case.get("case_state") or STATE_AWAITING_DOCTOR)
            self.store.transition_case(
                case_id, STATE_NEED_MORE_INFO, actor=reviewer_name or "doctor",
                reason=f"请求补资料：{item.title}", status="running")
            self.store.add_audit(case_id, "case_state_changed",
                                 f"{from_state} → {STATE_NEED_MORE_INFO}（请求补资料）")
            state_changed = STATE_NEED_MORE_INFO

        review = self.get_review(case_id) or {}
        return {"ok": True, "decision": decision.__dict__, "state_changed": state_changed,
                "items": review.get("items"), "summary": review.get("summary"),
                "pending_questions": review.get("pending_questions") or []}

    @serialized_case
    def publish_review(self, case_id: str, *, reviewer_name: str = "模拟审核员",
                       accept_all: bool = False) -> Dict[str, Any]:
        """发布审核版报告（屏 F 底部）：状态 → doctor_confirmed、版本 +1、角标转绿。

        规则（写死）：
        - 仍有"待审"条目时**不得发布**；`accept_all=True` 时把剩余条目**逐条**批量接受并留痕
          （不是静默通过：每条都落一条 reviewer=批量接受 的决策记录）；
        - 发布后病例状态 `doctor_confirmed`、`report_version + 1`、审计留痕。
        """
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return {"ok": False, "error": f"病例不存在：{case_id}"}

        symbols = self.get_review(case_id) or {}
        items = review_service.apply_decisions(
            review_service.build_items(case),
            self.store.list_decisions(case_id) if self.store else [])
        pending = [i for i in items if i.state == review_service.AWAITING]

        accepted_ids: List[str] = []
        if pending and accept_all:
            for item in pending:
                result = self.submit_decision(
                    case_id, item.item_id, "accept",
                    reason="批量接受（发布时统一确认）", reviewer_name=reviewer_name)
                if result.get("ok"):
                    accepted_ids.append(item.item_id)
            items = review_service.apply_decisions(
                review_service.build_items(case),
                self.store.list_decisions(case_id) if self.store else [])
            pending = [i for i in items if i.state == review_service.AWAITING]

        if pending:
            return {"ok": False,
                    "error": f"仍有 {len(pending)} 条未审核，不能发布审核版",
                    "pending_items": [i.item_id for i in pending]}

        summary = review_service.summarize(items)
        if not summary["total"]:
            return {"ok": False, "error": "该病例没有可审核条目，无法发布审核版"}

        payload = dict(case.get("payload") or {})
        new_version = int(case.get("report_version") or 1) + 1
        payload["review"] = {
            "published": True,
            "published_by": reviewer_name,
            "published_at": _now(),
            "version": new_version,
            "counts": summary["by_state"],
            "human_touched": summary["human_touched"],
            "batch_accepted": accepted_ids,
        }
        # "暂不确定"条目进入报告的待医生确认问题清单
        payload["review_pending_questions"] = review_service.pending_questions(items)
        if self.store:
            self.store.transition_case(
                case_id, "doctor_confirmed", actor=reviewer_name or "doctor",
                reason="发布审核版报告", status="reviewed",
                report_version=new_version, payload=payload)
            self.store.add_audit(
                case_id, "report_published",
                f"审核人={reviewer_name} 版本={new_version} 人工操作={summary['human_touched']}/"
                f"{summary['total']} 批量接受={len(accepted_ids)}")
        return {"ok": True, "case_state": "doctor_confirmed", "report_version": new_version,
                "summary": summary, "batch_accepted": accepted_ids,
                "review": payload["review"]}

    def get_review_history(self, case_id: str) -> Optional[Dict[str, Any]]:
        """屏 G 审核记录与版本：审计时间线 + 版本 + 条目状态快照。"""
        case = self.store.load_case(case_id) if self.store else None
        if not case:
            return None
        decisions = self.store.list_decisions(case_id) if self.store else []
        items = review_service.apply_decisions(review_service.build_items(case), decisions)
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
                 "created_at": d.get("created_at"), "version": d.get("version")}
                for d in decisions
            ],
            "items": [i.to_dict() for i in items],
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
                "masked_history": result.masked_preview.get("medical_history", ""),
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
