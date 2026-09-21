"""创建病例的**进度上报**与**两段式（MDT 后台续跑）**单元测试。

背景（真实体验问题）：点"确认脱敏并提交"后，旧实现把 3 次模型调用（画像增强 /
S2 受限发言 / 多专科意见卡 + 质询）串在同一次请求里，实测要等 30 秒才看到画像，
患者会以为卡死。现在：
  - `on_progress` 上报 8 个阶段，前端据此显示真实进度条；
  - `defer_mdt=True` 时先交付画像/追问/分诊/路径（约 3 秒），MDT 后台继续跑，
    进度写进 `payload["mdt_status"]`，`get_mdt_status()` 可查。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_create_progress -v
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402

CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往：2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。")


def wait_settled(flow, case_id, timeout=25.0):
    """等后台分析任务收尾（done / partial_failed / failed），返回最后一次状态。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = flow.get_analysis(case_id)
        if last and (last["analysis"].get("state") in ("done", "partial_failed", "failed")):
            time.sleep(0.1)          # 状态先落库、审计后写入：稍等，避免测试关库时撞上线程
            return last
        time.sleep(0.05)
    return last


class FakeAI:
    """假模型：画像/选科/专科意见卡/质询都返回可解析的 JSON。"""
    CARD = {
        "focus": "眼底与视网膜",
        "facts": ["视物模糊 3 月"],
        "inferences": [{"text": "需排查糖尿病视网膜病变", "confidence": "medium",
                        "evidence_refs": ["视物模糊 3 月"]}],
        "checks": [{"priority": "high", "content": "建议向医生确认是否需要完善眼底检查"}],
        "information_gaps": ["眼底检查：信息不足"],
    }

    def __init__(self):
        self.api_key = "fake-key"
        self.calls = []

    def call_ai_api(self, prompt, role_description=None):
        self.calls.append(role_description or "")
        text = str(prompt)
        if "选科" in text or "picks" in text:
            return json.dumps({"picks": []}, ensure_ascii=False)
        if "质询" in text or "challenge" in text.lower():
            return json.dumps({"questions": []}, ensure_ascii=False)
        if "画像" in text or "抽取" in text:
            return json.dumps({"chief_complaint": "视物模糊3月"}, ensure_ascii=False)
        return json.dumps(self.CARD, ensure_ascii=False)


class TestCreateProgress(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"), ai_service=FakeAI(), use_llm=True)

    def tearDown(self):
        self.flow.store.close()

    def test_progress_events_are_ordered_and_complete(self):
        events = []
        result = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                       age=58, on_progress=events.append)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(events, "应当上报阶段进度")

        percents = [e["percent"] for e in events]
        self.assertEqual(percents, sorted(percents), "进度必须单调不减")
        self.assertEqual(percents[-1], 100, "最后一个阶段必须是 100%")
        self.assertEqual([e["index"] for e in events], list(range(1, len(events) + 1)))

        stages = [e["stage"] for e in events]
        for expected in ("intake", "mask", "profile", "triage", "path", "mdt", "save"):
            self.assertIn(expected, stages, f"缺少阶段 {expected}")
        for event in events:
            self.assertTrue(event["message"], "每个阶段都要有人话说明")
            self.assertEqual(event["total"], len(self.flow.CREATE_STAGES))

    def test_progress_not_called_when_blocked(self):
        """被 G1/G2/G3 拦下时不应上报到后面的阶段（界面要停在录入页）。"""
        events = []
        result = self.flow.create_case(medical_history="", main_issue="", on_progress=events.append)
        self.assertFalse(result.ok)
        self.assertEqual([e["stage"] for e in events], ["intake"])

    def test_progress_callback_optional(self):
        """不传回调时行为不变（兼容旧调用）。"""
        result = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科")
        self.assertTrue(result.ok)


class TestDeferredMdt(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"), ai_service=FakeAI(), use_llm=True)
        self._cases = []

    def tearDown(self):
        # 后台线程可能还在跑：先等它结束再关库，避免 "closed database" 噪音
        for case_id in self._cases:
            self._wait_done(case_id, timeout=20.0)
        self.flow.store.close()

    def _create(self, **kwargs):
        result = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                       age=58, **kwargs)
        self._cases.append(result.case_id)
        return result

    def _wait_done(self, case_id, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.flow.get_mdt_status(case_id)
            if last and (last["ready"] or last["status"].get("state") in ("failed", "none")):
                return last                      # none = 本例没有后台任务（如同步模式）
            time.sleep(0.05)
        return last

    def test_defer_returns_without_mdt_but_with_pending_flag(self):
        result = self._create(defer_mdt=True)
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.mdt_pending, "两段式下应标记 MDT 仍在后台")
        self.assertIsNone(result.mdt, "两段式下主请求不应携带 MDT 结果")
        self.assertIs(result.to_dict()["mdt_pending"], True)
        # 画像/追问/分诊/路径必须已经就绪（患者马上要用）
        self.assertTrue(result.profile and result.triage and result.path)
        self.assertTrue(result.triage.get("selected"), "分诊方向应已给出")
        self.assertTrue(result.questions is not None)

    def test_background_mdt_writes_status_then_result(self):
        result = self._create(defer_mdt=True)
        status = self.flow.get_mdt_status(result.case_id)
        self.assertIsNotNone(status)
        self.assertIn(status["status"]["state"], ("pending", "running", "done"))
        self.assertTrue(status["status"]["total"] >= 1, "应知道要分析几个科")

        final = self._wait_done(result.case_id)
        self.assertIsNotNone(final)
        self.assertTrue(final["ready"], f"后台 MDT 未完成：{final}")
        self.assertEqual(final["status"]["state"], "done")
        mdt = final["mdt"] or {}
        self.assertTrue(mdt.get("analyses"), "完成后应有多专科意见卡")
        # 落库后的病例里也应看到结果（医生端/报告能直接用）
        case = self.flow.get_case(result.case_id)
        payload = case["payload"]
        self.assertTrue(payload.get("mdt"))
        # 新版后台任务把进度统一写在 payload["analysis"]（mdt_status 作为兼容字段保留读取）
        self.assertEqual((payload.get("analysis", {}).get("mdt") or {}).get("state"), "done")
        self.assertEqual((payload.get("analysis") or {}).get("state"), "done")

    def test_inline_mode_still_synchronous(self):
        """默认（defer_mdt=False）保持同步，便于测试与医生端批处理。"""
        result = self._create()
        self.assertFalse(result.mdt_pending)
        self.assertIsNotNone(result.mdt, "同步模式下应直接带 MDT 结果")

    def test_submit_answers_can_defer(self):
        created = self._create(defer_mdt=True)
        self._wait_done(created.case_id)
        out = self.flow.submit_answers(created.case_id, {"血糖控制水平": "控制不佳"}, defer_mdt=True)
        self.assertTrue(out["ok"], out)
        self.assertIn("mdt_pending", out)
        self.assertIsNotNone(self.flow.get_mdt_status(created.case_id))

    def test_mdt_status_unknown_case(self):
        self.assertIsNone(self.flow.get_mdt_status("C-NOT-EXIST"))


class TestProgressiveAnalysis(unittest.TestCase):
    """渐进式：先给规则结果（秒开），模型三相后台；失败可重试；单科跳过 MDT；医生端可批处理。"""

    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"), ai_service=FakeAI(), use_llm=True)
        self._cases = []

    def tearDown(self):
        for case_id in self._cases:
            self._wait_settled(case_id)
        self.flow.store.close()

    def _wait_settled(self, case_id, timeout=25.0):
        return wait_settled(self.flow, case_id, timeout)

    def _create(self, **kwargs):
        kwargs.setdefault("progressive", True)
        result = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                       age=58, **kwargs)
        self._cases.append(result.case_id)
        return result

    # ------------------------------------------------------------ 秒开
    def test_progressive_returns_rules_immediately(self):
        result = self._create()
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.progressive)
        self.assertTrue(result.analysis_pending)
        self.assertTrue(result.profile_is_rules_only, "首屏应为规则画像")
        # 规则结果必须已经可用（患者马上要看）
        self.assertTrue(result.profile and result.triage and result.path)
        self.assertTrue(result.questions)
        self.assertIsNone(result.mdt)

    def test_progressive_is_fast(self):
        """规则链路不调用模型，必须远快于模型链路（这里给个宽松上限防回归）。"""
        t0 = time.time()
        self._create()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 2.0, f"渐进式首屏耗时 {elapsed:.2f}s")

    def test_analysis_status_transitions(self):
        result = self._create()
        first = self.flow.get_analysis(result.case_id)
        self.assertEqual(first["analysis"]["state"], "running")
        self.assertEqual(set(first["analysis"]["phases"]), {"profile", "triage", "mdt"})
        self.assertEqual(first["case_state"], "analyzing")

        final = self._wait_settled(result.case_id)
        self.assertEqual(final["analysis"]["state"], "done")
        for phase in ("profile", "triage", "mdt"):
            self.assertEqual(final["analysis"]["phases"][phase]["state"], "done", phase)
        self.assertFalse(final["profile"].get("_rules_only"))
        self.assertTrue(final["mdt_ready"], "多学科结果应已就绪")
        self.assertEqual(final["case_state"], "awaiting_doctor", "分析完成后应推进到待医生审核")

    def test_model_results_replace_rules(self):
        result = self._create()
        rules_profile = result.profile
        final = self._wait_settled(result.case_id)
        self.assertIsNotNone(final["profile"])
        # 模型增强后仍是同一份画像结构（可被前端直接替换渲染）
        self.assertIn("chief_complaint", final["profile"])
        self.assertTrue(final["gaps"] is not None)
        self.assertEqual(rules_profile.get("case_id"), final["profile"].get("case_id"))

    # ------------------------------------------------------------ 单科按需跳过
    def test_single_department_skips_mdt(self):
        """只有一个就医方向时不该跑多学科会诊（如实标注"已跳过"，不假装分析过）。"""
        flow = CaseFlow(store=Store(":memory:"), ai_service=FakeAI(), use_llm=True)
        try:
            case_id = "C-SINGLE"
            flow.store.save_case(case_id, patient_name="", main_issue="发热",
                                 status="running", case_state="analyzing", report_version=1,
                                 payload={"masked_history": "患者：张*，男，30岁。主诉：发热3天。",
                                          "triage": {"selected": ["感染科"]},
                                          "analysis": CaseFlow._new_analysis_status(mdt_total=1)})
            flow.start_analysis_job(case_id, phases=["mdt"])
            deadline = time.time() + 20
            status = None
            while time.time() < deadline:
                status = (flow.get_analysis(case_id) or {})["analysis"]
                if status.get("state") != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(status["mdt"]["state"], "skipped", status)
            self.assertIn("仅 1 个就医方向", status["mdt"]["step"])
            self.assertIn("未运行多学科会诊",
                          str((flow.get_case(case_id)["payload"].get("mdt") or {}).get("aggregation")))
        finally:
            flow.store.close()

    # ------------------------------------------------------------ 失败与降级
    def test_broken_model_degrades_gracefully_with_warnings(self):
        """模型不可用时**不判失败**：相位照常收尾、规则结果保留，但必须如实说明。

        这是产品红线的一部分：宁可降级并说明，也不能因为模型问题让用户什么都拿不到。
        同时——某个科室没跑出来时**不能对外显示"全部完成"**，要标成部分未完成并可重试
        （真实故障：血液科意见卡被 max_tokens 截断，页面上永远"未完成分析"且无法重跑）。
        """
        class BrokenAI(FakeAI):
            def call_ai_api(self, prompt, role_description=None):
                raise RuntimeError("analysis-provider-secret")

        flow = CaseFlow(store=Store(":memory:"), ai_service=BrokenAI(), use_llm=True)
        try:
            result = flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                      age=58, progressive=True)
            deadline = time.time() + 20
            data = None
            while time.time() < deadline:
                data = flow.get_analysis(result.case_id)
                if data["analysis"]["state"] != "running":
                    break
                time.sleep(0.05)
            self.assertIn(data["analysis"]["state"], ("done", "partial_failed"),
                          data["analysis"])
            warnings = " ".join(data["analysis"]["warnings"])
            self.assertIn("模型增强失败", warnings, warnings)
            self.assertIn("S2 受限发言未生效", warnings, warnings)
            self.assertIn("专科分析未完成", warnings, warnings)
            # 降级后仍有可用的结果：规则画像 + 规则分诊 + 任务
            payload = flow.get_case(result.case_id)["payload"]
            self.assertTrue(payload.get("triage"))
            self.assertTrue(payload.get("profile"))
            self.assertTrue(payload.get("path"))
            # 一个科室都没跑出来 → mdt 相位如实记为 failed，且**可重试**
            self.assertIn(data["analysis"]["phases"]["mdt"]["state"], ("failed", "partial"))
            self.assertTrue(data["analysis"]["mdt"]["failed"], data["analysis"]["mdt"])
            retry = flow.retry_analysis(result.case_id)
            self.assertTrue(retry["ok"], retry)
            self.assertIn("mdt", retry["retried"])
            # 等重试的后台任务收尾再关库（否则线程会撞上已关闭的连接）
            deadline = time.time() + 20
            while time.time() < deadline:
                if (flow.get_analysis(result.case_id) or {})["analysis"]["state"] != "running":
                    break
                time.sleep(0.05)
        finally:
            flow.store.close()

    def test_phase_failure_is_recorded_and_retryable(self):
        """相位里出现**未捕获**的异常时：标记 failed + 写明原因 + 可重试（只重跑失败相位）。"""
        import services.case_flow as case_flow_module

        original_extract = case_flow_module.extract

        def boom(*args, **kwargs):
            if kwargs.get("use_llm"):            # 只让"模型增强"相位失败，规则抽取照常
                raise RuntimeError("profile-phase-secret")
            return original_extract(*args, **kwargs)

        case_flow_module.extract = boom
        try:
            result = self._create()
            deadline = time.time() + 20
            status = None
            while time.time() < deadline:
                status = (self.flow.get_analysis(result.case_id) or {})["analysis"]
                if status.get("state") != "running":
                    break
                time.sleep(0.05)
            self.assertIn(status["state"], ("failed", "partial_failed"), status)
            profile_phase = status["phases"]["profile"]
            self.assertEqual(profile_phase["state"], "failed")
            self.assertIn("阶段执行失败", profile_phase.get("note", ""))
            self.assertNotIn("profile-phase-secret", profile_phase.get("note", ""))
            self.assertTrue(any("结构化病例画像" in w for w in status["warnings"]), status["warnings"])
            # 其他相位仍应有结果（单相位失败不连坐）
            self.assertTrue(self.flow.get_case(result.case_id)["payload"].get("triage"))
            self.assertEqual(status["phases"]["triage"]["state"], "done")

            retry = self.flow.retry_analysis(result.case_id)
            self.assertTrue(retry["ok"], retry)
            self.assertEqual(retry["retried"], ["profile"], "只应重跑失败的那个相位")
        finally:
            case_flow_module.extract = original_extract

    def test_retry_noop_when_all_done(self):
        result = self._create()
        self._wait_settled(result.case_id)
        retry = self.flow.retry_analysis(result.case_id)
        self.assertFalse(retry["ok"])
        self.assertIn("无需重试", retry["error"])

    # ------------------------------------------------- 逐科失败（真实故障：血液科）
    def test_partial_mdt_failure_is_retryable(self):
        """单个科室跑不出来时：相位记 partial、病例记 partial_failed，且重试只重跑 mdt。

        真实故障：血液科意见卡被 max_tokens 截断 → 页面永远显示"本次未完成分析"，
        但相位算"完成"，于是"重新分析"回答"无需重试"，用户永远无法恢复。
        """

        class OneDeptBroken(FakeAI):
            """让"第 2 个科室"连续两次输出都不合法（模拟被截断/格式崩），重试时才恢复。"""

            def __init__(self):
                super().__init__()
                self.seen = []
                self.broken_dept = None
                self.broken_calls = 0

            def call_ai_api(self, prompt, role_description=None):
                role = role_description or ""
                if "AI 分析视角" in role:
                    dept = role.split("你是一位", 1)[-1].split("的 AI 分析视角")[0]
                    if self.broken_dept is None:
                        self.seen.append(dept)
                        if len(self.seen) == 2:
                            self.broken_dept = dept
                    if dept == self.broken_dept:
                        self.broken_calls += 1
                        if self.broken_calls <= 2:     # 首次分析的两次尝试都失败
                            return "这一科的输出坏了，根本不是 JSON"
                return super().call_ai_api(prompt, role_description)

        flow = CaseFlow(store=Store(":memory:"), ai_service=OneDeptBroken(), use_llm=True)
        try:
            case_id = "C-PARTIAL"
            analysis = CaseFlow._new_analysis_status(mdt_total=3)
            analysis["phases"]["profile"] = {"state": "done"}     # 渐进式下画像/分诊已完成
            analysis["phases"]["triage"] = {"state": "done"}
            flow.store.save_case(case_id, patient_name="", main_issue="该看哪个科",
                                 status="running", case_state="analyzing", report_version=1,
                                 payload={"masked_history": CASE_B,
                                          "triage": {"selected": ["内分泌科", "肾内科", "眼科"]},
                                          "analysis": analysis})
            flow.start_analysis_job(case_id, phases=["mdt"])
            status = wait_settled(flow, case_id)["analysis"]

            self.assertEqual(status["phases"]["mdt"]["state"], "partial", status)
            self.assertEqual(status["state"], "partial_failed")
            self.assertEqual(len(status["mdt"].get("failed") or []), 1, status["mdt"])
            self.assertEqual(status["mdt"]["done"], status["mdt"]["total"] - 1, status["mdt"])
            self.assertIn("可重试", status["phases"]["mdt"].get("note", ""))
            self.assertTrue(any("未完成" in w for w in status["warnings"]), status["warnings"])

            payload = flow.get_case(case_id)["payload"]
            self.assertEqual(payload["mdt"].get("failed_departments"), status["mdt"]["failed"])
            self.assertEqual(payload["mdt"].get("card_count"), 2, "其余科室的卡片必须保留")
            self.assertIn("未完成", str(payload["mdt"].get("aggregation")))

            retry = flow.retry_analysis(case_id)
            self.assertTrue(retry["ok"], retry)
            self.assertEqual(retry["retried"], ["mdt"], "只应重跑多学科相位")
            final = wait_settled(flow, case_id)["analysis"]
            self.assertEqual(final["phases"]["mdt"]["state"], "done", final)
            self.assertEqual(final["state"], "done", final)
            # 重跑成功后，旧的"某科未完成"告警必须被清掉（否则页面还在说未完成）
            self.assertFalse(any("未完成" in str(w) for w in final.get("warnings") or []),
                             final.get("warnings"))
        finally:
            flow.store.close()

    def test_batch_picks_up_partial_case(self):
        """医生端「批量补齐分析」必须认"有科室未完成"的病例，否则医生也修不了。"""
        case_id = "C-PARTIAL-BATCH"
        analysis = CaseFlow._new_analysis_status(mdt_total=3)
        analysis["state"] = "partial_failed"
        analysis["phases"]["profile"] = {"state": "done"}
        analysis["phases"]["triage"] = {"state": "done"}
        analysis["phases"]["mdt"] = {"state": "partial", "note": "血液科 未完成分析（可重试）"}
        analysis["mdt"] = {"state": "done", "done": 2, "total": 3, "current": "", "step": "",
                           "failed": ["血液科"]}
        self.flow.store.save_case(case_id, patient_name="", main_issue="该看哪个科",
                                  status="running", case_state="awaiting_doctor",
                                  report_version=1,
                                  payload={"masked_history": CASE_B,
                                           "triage": {"selected": ["内分泌科", "肾内科", "眼科"]},
                                           "analysis": analysis})
        self._cases.append(case_id)
        out = self.flow.analyze_pending_cases(limit=3)
        self.assertIn(case_id, out["started"], out)
        final = self._wait_settled(case_id)
        self.assertEqual(final["analysis"]["phases"]["mdt"]["state"], "done", final["analysis"])

    def test_legacy_stuck_case_can_be_retried(self):
        """修复前就卡住的病例（没有 analysis 记录，只有 mdt.analyses 里的 unavailable 条目）。

        这类病例在库里真实存在（3 例血液科失败），必须能被"重新分析"救回来，
        否则它们永远停在"某科未完成分析"。
        """
        case_id = "C-LEGACY-STUCK"
        self.flow.store.save_case(
            case_id, patient_name="", main_issue="该看哪个科", status="running",
            case_state="awaiting_doctor", report_version=1,
            payload={"masked_history": CASE_B,
                     "triage": {"selected": ["内分泌科", "肾内科", "眼科"]},
                     "mdt": {"departments": ["内分泌科", "肾内科", "眼科"],
                             "analyses": [
                                 {"department": "内分泌科", "status": "ok", "focus": "血糖控制"},
                                 {"department": "血液科", "status": "unavailable",
                                  "method": "failed",
                                  "error": "意见卡解析失败：Expecting ',' delimiter"},
                             ],
                             "aggregation": {"consensus": [], "disagreements": [],
                                             "uncertainties": [], "open_questions": [],
                                             "notes": []},
                             "card_count": 1, "challenge": {}}})
        self._cases.append(case_id)
        retry = self.flow.retry_analysis(case_id)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["retried"], ["mdt"], "老病例只应重跑多学科相位")
        final = self._wait_settled(case_id)
        self.assertEqual(final["analysis"]["phases"]["mdt"]["state"], "done", final["analysis"])

    # ------------------------------------------------------------ 医生端批处理
    def test_analyze_pending_cases(self):
        # 造一个"只有规则结果、分析未跑"的病例
        case_id = "C-PENDING"
        self.flow.store.save_case(case_id, patient_name="", main_issue="该看哪个科",
                                  status="running", case_state="analyzing", report_version=1,
                                  payload={"masked_history": CASE_B,
                                           "profile": {"chief_complaint": "视物模糊3月"},
                                           "triage": {"selected": ["内分泌科", "肾内科", "眼科"]},
                                           "path": {"steps": []}})
        self._cases.append(case_id)
        out = self.flow.analyze_pending_cases(limit=2)
        self.assertTrue(out["ok"])
        self.assertIn(case_id, out["started"], out)
        final = self._wait_settled(case_id)
        self.assertEqual(final["analysis"]["state"], "done")

    def test_analyze_pending_skips_running_and_done(self):
        result = self._create()
        out = self.flow.analyze_pending_cases(limit=5)
        self.assertIn(result.case_id, out["skipped"], "已在分析中的病例不应重复排队")

    def test_analysis_for_unknown_case(self):
        self.assertIsNone(self.flow.get_analysis("C-NOT-EXIST"))
        self.assertFalse(self.flow.retry_analysis("C-NOT-EXIST")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
