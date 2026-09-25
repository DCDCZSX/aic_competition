"""S2 受限发言与多专科分析/聚合的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_mdt_analyst -v

全部使用**假 ai_service**，不消耗真实 token；真实 LLM 链路另行手工验证。
验收对应规格书 §4.3（S2 受限发言）、任务 2.7（意见卡+聚合）、2.8（分歧检测）。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CheckItem, Inference, SpecialistOpinionCard  # noqa: E402
from services.mdt_analyst import (  # noqa: E402
    MDT_MAX_TOKENS,
    METHOD_FAILED,
    METHOD_LLM,
    METHOD_LLM_REPAIRED,
    METHOD_SKIPPED,
    SpecialtyAnalysis,
    aggregate,
    analyze_specialty,
    canonical_topic,
    detect_disagreements,
)
from services.rule_engine import evaluate  # noqa: E402
from services.s2_analyst import (  # noqa: E402
    METHOD_SKIPPED as S2_SKIPPED,
    ModelPick,
    analyze as s2_analyze,
)

CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。")


class FakeAI:
    """可编排的假模型。"""

    def __init__(self, reply=None, api_key="fake-key"):
        self.reply = reply
        self.api_key = api_key
        self.calls = []
        self.roles = []

    def call_ai_api(self, prompt, role_description=None):
        self.calls.append(prompt)
        self.roles.append(role_description)
        if callable(self.reply):
            return self.reply(prompt)
        return self.reply


class BrokenAI(FakeAI):
    def call_ai_api(self, prompt, role_description=None):
        raise RuntimeError("网关不可用")


class FakeRawAI(FakeAI):
    """只提供**结构化接口**的假模型（与真实 AIService 的行为一致）。"""

    def __init__(self, replies, api_key="fake-key"):
        super().__init__(reply=None, api_key=api_key)
        self.replies = list(replies)          # [(正文, 是否被截断), ...]
        self.budgets = []

    def call_ai_api_raw(self, prompt, role_description=None, max_tokens=None):
        self.calls.append(prompt)
        self.roles.append(role_description)
        self.budgets.append(max_tokens)
        content, truncated = self.replies.pop(0) if self.replies else ("", False)
        return {"ok": True, "content": content,
                "finish_reason": "length" if truncated else "stop",
                "truncated": truncated, "error": ""}


def card(dept, checks=(), gaps=(), inferences=(), focus=""):
    return SpecialistOpinionCard(
        case_id="C1", specialty_code=dept, specialty_name=dept, focus=focus,
        checks=[CheckItem(priority=p, content=c) for p, c in checks],
        information_gaps=list(gaps),
        inferences=[Inference(text=t, confidence=c) for t, c in inferences],
    )


class TestS2RestrictedSpeech(unittest.TestCase):
    def setUp(self):
        self.evaluation = evaluate(CASE_B)

    def test_no_key_skips(self):
        result = s2_analyze(ai_service=FakeAI(api_key=""), text=CASE_B,
                            evaluation=self.evaluation)
        self.assertEqual(result.method, S2_SKIPPED)
        self.assertEqual(result.picks, [])

    def test_none_service_skips(self):
        result = s2_analyze(ai_service=None, text=CASE_B, evaluation=self.evaluation)
        self.assertEqual(result.method, S2_SKIPPED)

    def test_prompt_contains_rule_candidates(self):
        """受限发言的关键：上下文里必须已有规则清单与理由。"""
        ai = FakeAI(reply='{"picks": []}')
        s2_analyze(ai_service=ai, text=CASE_B, evaluation=self.evaluation,
                   main_issue="该看哪个科")
        prompt = ai.calls[0]
        self.assertIn("规则引擎已选出的候选科室", prompt)
        self.assertIn("内分泌科", prompt)
        self.assertIn("strong", prompt)
        self.assertIn("不得自造", prompt)

    def test_parse_picks(self):
        reply = json.dumps({"picks": [
            {"department": "内分泌科", "stance": "accept", "reason": "糖尿病基础病"},
            {"department": "眼科", "stance": "supplement", "confidence": 0.8, "reason": "视物模糊"},
            {"department": "皮肤科", "stance": "reject", "reason": "无皮疹"},
        ]}, ensure_ascii=False)
        result = s2_analyze(ai_service=FakeAI(reply=reply), text=CASE_B,
                            evaluation=self.evaluation)
        self.assertEqual(result.method, METHOD_LLM)
        self.assertEqual(len(result.picks), 3)
        self.assertEqual(result.rejected[0].department, "皮肤科")

    def test_out_of_domain_filtered(self):
        reply = json.dumps({"picks": [
            {"department": "口腔颌面外科", "stance": "supplement", "confidence": 0.9},
            {"department": "眼科", "stance": "accept"},
        ]}, ensure_ascii=False)
        result = s2_analyze(ai_service=FakeAI(reply=reply), text=CASE_B,
                            evaluation=self.evaluation)
        self.assertEqual([p.department for p in result.picks], ["眼科"])
        self.assertIn("口腔颌面外科", result.error)

    def test_bad_json_degrades(self):
        result = s2_analyze(ai_service=FakeAI(reply="这不是 JSON"), text=CASE_B,
                            evaluation=self.evaluation)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertEqual(result.picks, [])

    def test_parser_exception_detail_is_not_returned(self):
        secret = "parser-secret-detail"
        with mock.patch("services.s2_analyst._parse_picks",
                        side_effect=ValueError(secret)):
            result = s2_analyze(ai_service=FakeAI(reply='{"picks": []}'),
                                text=CASE_B, evaluation=self.evaluation)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertNotIn(secret, result.error)

    def test_broken_service_degrades(self):
        result = s2_analyze(ai_service=BrokenAI(), text=CASE_B, evaluation=self.evaluation)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertEqual(result.picks, [])

    def test_confidence_clamped(self):
        reply = json.dumps({"picks": [
            {"department": "眼科", "stance": "supplement", "confidence": 5},
        ]}, ensure_ascii=False)
        result = s2_analyze(ai_service=FakeAI(reply=reply), text=CASE_B,
                            evaluation=self.evaluation)
        self.assertEqual(result.picks[0].confidence, 1.0)

    def test_orchestrator_pick_shape(self):
        accept = ModelPick("内分泌科", "accept", None, "理由")
        reject = ModelPick("皮肤科", "reject", None, "无皮疹")
        self.assertNotIn("stance", accept.to_orchestrator_pick())
        self.assertEqual(reject.to_orchestrator_pick()["stance"], "reject")


class TestSpecialtyCard(unittest.TestCase):
    CARD_JSON = json.dumps({
        "focus": "糖尿病视网膜病变筛查",
        "facts": ["视物模糊3月", "2型糖尿病5年"],
        "inferences": [{"text": "需排查糖尿病视网膜病变", "confidence": "medium",
                        "evidence_refs": ["视物模糊"]}],
        "checks": [{"priority": "high", "content": "行眼底照相检查"}],
        "information_gaps": ["无既往眼底记录"],
        "referral": None,
    }, ensure_ascii=False)

    def test_serialized_specialty_content_has_ai_source_marker(self):
        result = SpecialtyAnalysis(department="眼科", method=METHOD_SKIPPED)
        data = result.to_dict()
        self.assertEqual(data["source_type"], "ai_reference")
        self.assertEqual(data["source_label"], "专科 AI 参考意见")

    def test_analyze_ok(self):
        result = analyze_specialty(ai_service=FakeAI(reply=self.CARD_JSON),
                                   department="眼科", text=CASE_B, case_id="C1")
        self.assertEqual(result.method, METHOD_LLM)
        self.assertIsNotNone(result.card)
        self.assertEqual(result.card.specialty_name, "眼科")
        self.assertEqual(result.card.focus, "糖尿病视网膜病变筛查")

    def test_checks_forced_to_confirm_style(self):
        """红线：模型给的检查建议必须转成"建议向医生确认是否需要…"。"""
        reply = json.dumps({"checks": [{"priority": "high", "content": "立即行眼底照相"}]},
                           ensure_ascii=False)
        result = analyze_specialty(ai_service=FakeAI(reply=reply),
                                   department="眼科", text=CASE_B)
        self.assertTrue(result.card.checks[0].content.startswith("建议向医生确认是否需要"))

    def test_out_of_domain_referral_dropped(self):
        reply = json.dumps({"focus": "眼部症状评估", "referral": "口腔颌面外科"},
                           ensure_ascii=False)
        result = analyze_specialty(ai_service=FakeAI(reply=reply),
                                   department="眼科", text=CASE_B)
        self.assertIsNotNone(result.card)
        self.assertIsNone(result.card.referral, "域外转诊方向应留空而非编造")

    def test_invalid_department_rejected(self):
        result = analyze_specialty(ai_service=FakeAI(reply=self.CARD_JSON),
                                   department="口腔颌面外科", text=CASE_B)
        self.assertEqual(result.method, METHOD_FAILED)

    def test_prompt_is_independent(self):
        """第一轮独立研判：提示词里不得出现其他科室的结论。"""
        ai = FakeAI(reply=self.CARD_JSON)
        analyze_specialty(ai_service=ai, department="眼科", text=CASE_B)
        prompt = ai.calls[0]
        role = ai.roles[0] or ""
        self.assertNotIn("肾内科的结论", prompt)
        self.assertIn("不是真实医生", role)            # 定位声明（system prompt）
        self.assertIn("不得给出诊断结论", prompt)      # 红线

    def test_broken_service(self):
        result = analyze_specialty(ai_service=BrokenAI(), department="眼科", text=CASE_B)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertIsNone(result.card)

    def test_parser_exception_detail_is_not_returned(self):
        secret = "specialty-parser-secret"
        with mock.patch("services.mdt_analyst._parse_or_raise",
                        side_effect=ValueError(secret)):
            result = analyze_specialty(
                ai_service=FakeAI(reply=self.CARD_JSON), department="眼科", text=CASE_B
            )
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertNotIn(secret, result.error)

    def test_empty_object_is_failed_not_a_successful_card(self):
        result = analyze_specialty(ai_service=FakeAI(reply="{}"),
                                   department="眼科", text=CASE_B, case_id="C1")
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertIsNone(result.card)
        self.assertIn("意见卡校验失败", result.error)


class TestMalformedModelOutput(unittest.TestCase):
    """真实故障回归：血液科意见卡被 max_tokens 截断 → 整科被丢弃。

    现场证据（deepseek-chat）：`finish_reason=length`、输出 3780 字符、`json.loads`
    报 `Expecting ',' delimiter: line 64 column 6 (char 3537)`；库里有 3 例同样失败。
    """

    CARD = {
        "focus": "全血细胞减少伴发热的血液系统病因排查",
        "facts": ["35岁女性，咳嗽2月余", "血小板 43×10^9/L"],
        "inferences": [{"text": "需排查噬血细胞综合征", "confidence": "medium",
                        "evidence_refs": ["铁蛋白"]}],
        "checks": [{"priority": "high", "content": "建议向医生确认是否需要骨髓穿刺"}],
        "information_gaps": ["铁蛋白具体数值"],
        "referral": None,
    }
    # 被截断的样子：少收尾括号、最后一条字符串没写完
    TRUNCATED = (
        '{\n  "focus": "全血细胞减少伴发热、脾大、sCD25升高的血液系统病因排查",\n'
        '  "facts": [\n    "35岁女性，咳嗽2月余、胸闷气促",\n    "血小板 43×10^9/L",\n'
        '    "铁蛋白升高、甘油三酯升高",\n    "NK细胞活性检测结果",\n'
        '    "EBV、CMV等病毒DNA及抗体'
    )

    def test_budget_is_raised_and_uses_structured_call(self):
        """结构化输出必须走 call_ai_api_raw（不做 Markdown 清理）并带足预算。"""
        ai = FakeRawAI([(json.dumps(self.CARD, ensure_ascii=False), False)])
        analyze_specialty(ai_service=ai, department="血液科", text=CASE_B)
        self.assertEqual(ai.budgets, [MDT_MAX_TOKENS])
        self.assertGreaterEqual(MDT_MAX_TOKENS, 4000, "血液科需要 ~2335 token，2000 会被截断")

    def test_truncated_output_is_recovered_and_flagged(self):
        ai = FakeRawAI([(self.TRUNCATED, True)])
        result = analyze_specialty(ai_service=ai, department="血液科", text=CASE_B)
        self.assertIsNotNone(result.card, result.error)
        self.assertEqual(result.method, METHOD_LLM_REPAIRED)
        self.assertIn("截断", result.error)
        self.assertEqual(result.card.facts[:3], ["35岁女性，咳嗽2月余、胸闷气促",
                                                 "血小板 43×10^9/L",
                                                 "铁蛋白升高、甘油三酯升高"])
        self.assertNotIn("EBV、CMV等病毒DNA及抗体", result.card.facts, "半个字符串不能入库")
        # 医生端要能看到"这条是修复来的"
        self.assertEqual(result.to_dict()["notice"], result.error)

    def test_terse_retry_after_truncation(self):
        """被截断时先按"更精炼"的要求重试一次；成功则算正常结果（不标修复）。"""
        ai = FakeRawAI([(self.TRUNCATED, True),
                        (json.dumps(self.CARD, ensure_ascii=False), False)])
        result = analyze_specialty(ai_service=ai, department="血液科", text=CASE_B)
        self.assertEqual(result.method, METHOD_LLM)
        self.assertEqual(result.error, "")
        self.assertEqual(len(ai.budgets), 2)
        self.assertIn("上一次输出因为太长被截断了", ai.calls[1])
        self.assertIn("最多 5 条", ai.calls[1])

    def test_fullwidth_punctuation_card(self):
        raw = '{"focus"："血细胞异常"， "facts"：["血小板 43×10^9/L"]， "referral"：null}'
        result = analyze_specialty(ai_service=FakeRawAI([(raw, False)]),
                                   department="血液科", text=CASE_B)
        self.assertIsNotNone(result.card)
        self.assertEqual(result.card.facts, ["血小板 43×10^9/L"])
        self.assertEqual(result.method, METHOD_LLM_REPAIRED)

    def test_unrecoverable_output_fails_with_honest_reason(self):
        result = analyze_specialty(ai_service=FakeRawAI([("模型今天不想说话", False)]),
                                   department="血液科", text=CASE_B)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertIsNone(result.card)
        self.assertIn("意见卡解析失败", result.error)

    def test_truncation_is_named_in_failure_reason(self):
        """连恢复都失败时，原因里必须写明"输出被长度截断"（而不是含糊的 JSON 错误）。"""
        result = analyze_specialty(ai_service=FakeRawAI([("{\n  \"facts\": [", True)]),
                                   department="血液科", text=CASE_B)
        self.assertEqual(result.method, METHOD_FAILED)
        self.assertIn("截断", result.error)


class TestTopicNormalization(unittest.TestCase):
    def test_same_topic_different_wording(self):
        a = canonical_topic("建议向医生确认是否需要做眼底检查（如眼底照相）")
        b = canonical_topic("建议向医生确认是否需要做散瞳后眼底检查以评估视网膜")
        self.assertEqual(a, b)
        self.assertEqual(a, "眼底检查")

    def test_unclassified_returns_none(self):
        self.assertIsNone(canonical_topic("多与家属沟通病情"))


class TestAggregation(unittest.TestCase):
    def test_consensus_merges_wording(self):
        cards = [
            card("内分泌科", [("high", "建议向医生确认是否需要做眼底检查（如眼底照相）")]),
            card("眼科", [("high", "建议向医生确认是否需要做散瞳后眼底检查")]),
        ]
        agg = aggregate(cards)
        topics = [c["topic"] for c in agg.consensus]
        self.assertIn("眼底检查", topics)
        entry = [c for c in agg.consensus if c["topic"] == "眼底检查"][0]
        self.assertEqual(sorted(entry["supporters"]), ["内分泌科", "眼科"])
        self.assertNotIn("眼底检查", [u["topic"] for u in agg.uncertainties])

    def test_single_department_goes_to_uncertainty(self):
        cards = [card("眼科", [("high", "建议向医生确认是否需要做眼底检查")])]
        agg = aggregate(cards)
        self.assertEqual(agg.consensus, [])
        self.assertIn("眼底检查", [u["topic"] for u in agg.uncertainties])

    def test_disagreement_detected(self):
        cards = [
            card("心血管内科", [("high", "建议向医生确认是否需要手术评估")]),
            card("肾内科", [("high", "建议保守治疗，暂不手术")]),
        ]
        agg = aggregate(cards)
        self.assertEqual(len(agg.disagreements), 1)
        self.assertIn("分歧待裁决", " ".join(agg.open_questions))

    def test_same_direction_not_disagreement(self):
        cards = [
            card("心血管内科", [("high", "建议手术评估")]),
            card("心胸外科", [("high", "建议手术评估，具备手术指征")]),
        ]
        self.assertEqual(detect_disagreements(cards), [])

    def test_low_confidence_inference_is_uncertain(self):
        cards = [card("眼科", inferences=[("可能为视网膜病变", "low")])]
        agg = aggregate(cards)
        self.assertTrue(any("低置信" in str(u["impact"]) for u in agg.uncertainties))

    def test_open_questions_include_high_checks(self):
        cards = [card("眼科", [("high", "建议向医生确认是否需要做眼底检查"),
                               ("low", "建议向医生确认是否需要补充病史")])]
        agg = aggregate(cards)
        self.assertEqual(len(agg.open_questions), 1)
        self.assertIn("眼底检查", agg.open_questions[0])

    def test_empty_cards_note(self):
        agg = aggregate([])
        self.assertTrue(agg.notes)
        self.assertEqual(agg.consensus, [])

    def test_rejections_recorded_in_notes(self):
        agg = aggregate([card("眼科", [("high", "建议向医生确认是否需要做眼底检查")])],
                        rejections=["眼科"])
        self.assertTrue(any("反驳" in n for n in agg.notes))

    def test_no_scores_or_probabilities_in_output(self):
        """红线：聚合结果不得出现概率数字。"""
        cards = [card("眼科", [("high", "建议向医生确认是否需要做眼底检查")]),
                 card("内分泌科", [("high", "建议向医生确认是否需要做眼底检查")])]
        text = json.dumps(aggregate(cards).to_dict(), ensure_ascii=False)
        for token in ("%", "概率", "置信度", "confidence"):
            self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
