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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CheckItem, Inference, SpecialistOpinionCard  # noqa: E402
from services.mdt_analyst import (  # noqa: E402
    METHOD_FAILED,
    METHOD_LLM,
    METHOD_SKIPPED,
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
        reply = json.dumps({"referral": "口腔颌面外科"}, ensure_ascii=False)
        result = analyze_specialty(ai_service=FakeAI(reply=reply),
                                   department="眼科", text=CASE_B)
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
