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
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.ask_service import Question, apply_answers, build_questions  # noqa: E402
from services import challenge_service  # noqa: E402
from services.guardrail import screen  # noqa: E402
from services.intake import IntakeResult, sanitized_payload, validate_intake  # noqa: E402
from services.mdt_analyst import aggregate, analyze_specialty  # noqa: E402
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
            "s2": self.s2,
        }


class CaseFlow:
    """患者端病例流程编排。"""

    def __init__(self, store: Optional[Store] = None, ai_service: Any = None,
                 use_llm: bool = False) -> None:
        self.store = store
        self.ai_service = ai_service
        self.use_llm = use_llm

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
                 rejections: Optional[List[str]] = None) -> Dict[str, Any]:
        """对入选科室做**第一轮独立研判**（互不预读）并聚合（P8）。

        每个专科单独调用、提示词里不含其他科室结论 —— 防锚定（主文档 §8.1）。
        """
        departments = outcome.selected[:MAX_MDT_DEPARTMENTS]
        analyses = []
        for dep in departments:
            a = analyze_specialty(
                ai_service=self.ai_service, department=dep, text=text,
                profile=profile, case_id=case_id, answers=answers,
            )
            analyses.append(a)

        cards = [a.card for a in analyses if a.card is not None]
        aggregation = aggregate(cards, rejections=rejections or [])
        failed = [a.department for a in analyses if a.card is None]
        if failed:
            aggregation.notes.append(f"以下专科分析未完成（已跳过，不阻塞）：{'、'.join(failed)}")

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
        }

    # ------------------------------------------------------------------ 创建
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
    ) -> CaseCreation:
        """执行"录入 → 校验 → 脱敏 → 画像 → 追问"的前半段链路。"""
        result = CaseCreation()

        # ---- 1. 录入校验（含 G1 危象 / G2 双空 / G3 PII）----
        intake: IntakeResult = validate_intake(
            medical_history=medical_history, main_issue=main_issue,
            goals=goals, age=age, sex=sex, name=name,
        )
        result.errors = list(intake.errors)
        result.warnings = list(intake.warnings)
        result.goals = list(intake.goals)

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

        if not extraction.passes_gate:
            # 门禁未过：阻断进入分析，回画像确认补录（不再往下走）
            result.ok = False
            result.case_state = STATE_NEED_MORE_INFO
            self._save_case(result, age=age, sex=sex)
            return result

        # ---- 4. 智能追问（P5）----
        questions: List[Question] = build_questions(gaps=result.gaps)
        result.questions = [q.to_dict() for q in questions]

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

        # ---- 6. 就医路径与任务（P7）----
        checks_by_dept = self._checks_by_department(result.gaps, outcome.selected)
        care_path = build_path(outcome=outcome, gaps=result.gaps,
                               checks_by_department=checks_by_dept, case_id=case_id)
        result.path = care_path.to_dict()

        # ---- 7. 多专科独立研判 + 聚合（P8）----
        if self.use_llm and self.ai_service is not None and outcome.selected:
            result.mdt = self._run_mdt(
                text=masked_history, outcome=outcome, profile=result.profile,
                case_id=case_id, rejections=[c.get("department") for c in outcome.conflicts],
            )

        result.case_state = STATE_AWAITING_DOCTOR

        if not outcome.selected:
            result.warnings.append("未能给出明确的就诊方向建议，请补充资料或线下咨询医生")

        result.ok = True
        self._save_case(result, age=age, sex=sex)
        return result

    # ------------------------------------------------------------------ 追问回答
    def submit_answers(self, case_id: str, answers: Dict[str, str]) -> Dict[str, Any]:
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
        if self.use_llm and self.ai_service is not None and new_selected != old_selected:
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
        }

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
            self.store.save_case(
                case_id, patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "", status=case.get("status") or "running",
                case_state="awaiting_recheck",
                report_version=int(case.get("report_version") or 1), payload=payload)
            self.store.save_transition(case_id, case.get("case_state") or STATE_AWAITING_DOCTOR,
                                       "awaiting_recheck", actor=source,
                                       reason=f"{kind}更新")
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
    def list_review_cases(self, limit: int = 100, state_filter: str = "") -> List[Dict[str, Any]]:
        """屏 A 病例列表：按"待审优先"排序，带待审条数与关键摘要。"""
        if not self.store:
            return []
        rows: List[Dict[str, Any]] = []
        for case in self.store.list_cases(limit=limit):
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

    def submit_decision(self, case_id: str, item_id: str, action: str, *,
                        after: str = "", reason: str = "",
                        reviewer_name: str = "审核医生", reviewer_dept: str = "") -> Dict[str, Any]:
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
            record["reviewer"] = {"role": reviewer_name or "审核医生", "dept": reviewer_dept}
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
            self.store.save_case(
                case_id, patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "", status="running",
                case_state=STATE_NEED_MORE_INFO,
                report_version=int(case.get("report_version") or 1),
                payload=dict(case.get("payload") or {}))
            self.store.save_transition(case_id, from_state, STATE_NEED_MORE_INFO,
                                       actor=reviewer_name or "doctor",
                                       reason=f"请求补资料：{item.title}")
            self.store.add_audit(case_id, "case_state_changed",
                                 f"{from_state} → {STATE_NEED_MORE_INFO}（请求补资料）")
            state_changed = STATE_NEED_MORE_INFO

        review = self.get_review(case_id) or {}
        return {"ok": True, "decision": decision.__dict__, "state_changed": state_changed,
                "items": review.get("items"), "summary": review.get("summary"),
                "pending_questions": review.get("pending_questions") or []}

    def publish_review(self, case_id: str, *, reviewer_name: str = "审核医生",
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
            self.store.save_case(
                case_id, patient_name=case.get("patient_name") or "",
                main_issue=case.get("main_issue") or "", status="reviewed",
                case_state="doctor_confirmed", report_version=new_version, payload=payload)
            self.store.save_transition(case_id, str(case.get("case_state") or STATE_AWAITING_DOCTOR),
                                       "doctor_confirmed", actor=reviewer_name or "doctor",
                                       reason="发布审核版报告")
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
                 "target_type": d.get("target_type"), "target_id": d.get("target_id"),
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

    def _save_case(self, result: CaseCreation, *, age: Any = None, sex: str = "") -> None:
        if not self.store:
            return
        self.store.save_case(
            result.case_id,
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
