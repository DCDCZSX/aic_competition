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
        self.assertIn(status["status"]["state"], ("running", "done"))
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
        self.assertEqual((payload.get("mdt_status") or {}).get("state"), "done")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
