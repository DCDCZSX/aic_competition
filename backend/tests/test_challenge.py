"""轻量质询（P0）的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_challenge -v

验收对应规格书 §6.3：质询产出结构完整率 100%，且**不得出现概率数字**。
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import CHALLENGE_OUTCOMES, CHALLENGE_STANCES, CheckItem, SpecialistOpinionCard  # noqa: E402
from services import challenge_service as cs  # noqa: E402


class FakeAI:
    def __init__(self, reply):
        self.reply = reply
        self.api_key = "fake"
        self.calls = []

    def call_ai_api(self, prompt, role_description=None):
        self.calls.append(prompt)
        return self.reply


def card(dept, gaps=()):
    return SpecialistOpinionCard(
        case_id="C1", specialty_code=dept, specialty_name=dept,
        checks=[CheckItem(priority="high", content="建议向医生确认是否需要做眼底检查")],
        information_gaps=list(gaps),
    )


DISAGREEMENT = {
    "disagreements": [{"topic": "建议手术 vs 保守治疗",
                       "positions": {"心血管内科": "手术评估", "肾内科": "保守治疗"}}],
    "uncertainties": [],
}


class TestQuestionBuilding(unittest.TestCase):
    def test_no_trigger_when_no_disagreement(self):
        self.assertEqual(cs.build_questions({"disagreements": [], "uncertainties": []}), [])

    def test_disagreement_triggers_question(self):
        qs = cs.build_questions(DISAGREEMENT, [])
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0].kind, "disagreement")
        self.assertEqual(set(qs[0].targets), {"心血管内科", "肾内科"})
        self.assertIn("建议手术", qs[0].question)

    def test_shared_gap_triggers_question(self):
        cards = [card("眼科", ["眼底检查结果"]), card("内分泌科", ["眼底检查结果"])]
        qs = cs.build_questions({"disagreements": []}, cards)
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0].kind, "gap")
        self.assertIn("眼底检查结果", qs[0].question)

    def test_single_department_gap_does_not_trigger(self):
        cards = [card("眼科", ["眼底检查结果"])]
        self.assertEqual(cs.build_questions({"disagreements": []}, cards), [])

    def test_max_three_questions(self):
        agg = {"disagreements": [
            {"topic": f"分歧{i}", "positions": {"心血管内科": "手术", "肾内科": "保守"}}
            for i in range(6)
        ]}
        self.assertLessEqual(len(cs.build_questions(agg, [])), cs.MAX_CHALLENGES)


class TestRunWithoutModel(unittest.TestCase):
    def test_skipped_but_questions_produced(self):
        result = cs.run(ai_service=None, aggregation=DISAGREEMENT, cards=[])
        self.assertTrue(result.triggered)
        self.assertEqual(result.rounds, [])
        self.assertIn("未配置模型", result.skipped_reason)

    def test_no_questions_means_no_run(self):
        result = cs.run(ai_service=None,
                        aggregation={"disagreements": [], "uncertainties": []}, cards=[])
        self.assertFalse(result.triggered)
        self.assertIn("无需质询", result.skipped_reason)


class TestRunWithModel(unittest.TestCase):
    REPLY = json.dumps({"stance": "补充证据",
                        "reason": "需要补充眼底检查结果才能确认",
                        "follow_up_evidence": ["眼底照相", "糖化血红蛋白"]},
                       ensure_ascii=False)

    def test_rounds_produced_and_valid(self):
        result = cs.run(ai_service=FakeAI(self.REPLY), aggregation=DISAGREEMENT,
                        cards=[], text="患者资料")
        self.assertEqual(result.method, cs.METHOD_LLM)
        self.assertEqual(len(result.rounds), 1)
        round_ = result.rounds[0]
        self.assertEqual(len(round_.responses), 2)
        for r in round_.responses:
            self.assertIn(r["stance"], CHALLENGE_STANCES)
            self.assertEqual(r["specialty"], r["specialty"] if r["specialty"] else "")
        self.assertIn(round_.outcome, CHALLENGE_OUTCOMES)
        self.assertTrue(round_.follow_up_evidence)
        round_.validate()   # 结构校验

    def test_outcome_needs_doctor_decision(self):
        """双方都维持原判断且是分歧 → 交医生裁决。"""
        reply = json.dumps({"stance": "维持原判断", "reason": "依据充分"}, ensure_ascii=False)
        result = cs.run(ai_service=FakeAI(reply), aggregation=DISAGREEMENT, cards=[])
        self.assertEqual(result.rounds[0].outcome, "需医生裁决")
        self.assertTrue(result.needs_doctor_decision)

    def test_outcome_revised(self):
        reply = json.dumps({"stance": "修正观点", "reason": "重新评估后同意对方"}, ensure_ascii=False)
        result = cs.run(ai_service=FakeAI(reply), aggregation=DISAGREEMENT, cards=[])
        self.assertEqual(result.rounds[0].outcome, "修正观点")

    def test_reason_from_previous_judgement_included(self):
        ai = FakeAI(self.REPLY)
        cs.run(ai_service=ai, aggregation=DISAGREEMENT, cards=[], text="资料",
               card_reasons={"心血管内科": "考虑手术指征"})
        self.assertTrue(any("考虑手术指征" in c for c in ai.calls))

    def test_bad_json_degrades_to_maintain(self):
        result = cs.run(ai_service=FakeAI("不是 JSON"), aggregation=DISAGREEMENT, cards=[])
        self.assertEqual(result.rounds[0].responses[0]["stance"], "维持原判断")

    def test_prompt_forbids_probability(self):
        ai = FakeAI(self.REPLY)
        cs.run(ai_service=ai, aggregation=DISAGREEMENT, cards=[])
        self.assertIn("禁止输出任何置信度、概率或百分比数字", ai.calls[0])


class TestRedlines(unittest.TestCase):
    def test_output_has_no_probability(self):
        """红线：质询产出不得出现概率/置信度数字（主文档 §8.4）。"""
        reply = json.dumps({"stance": "补充证据", "reason": "依据不足",
                            "follow_up_evidence": ["眼底照相"]}, ensure_ascii=False)
        result = cs.run(ai_service=FakeAI(reply), aggregation=DISAGREEMENT, cards=[])
        text = json.dumps(result.to_dict(), ensure_ascii=False)
        for token in ("%", "概率", "置信度", "confidence", "可能性 80"):
            self.assertNotIn(token, text)

    def test_outcome_enum_only(self):
        for kind in ("disagreement", "gap"):
            for stance in CHALLENGE_STANCES:
                outcome = cs._decide_outcome([{"stance": stance}], kind)
                self.assertIn(outcome, CHALLENGE_OUTCOMES)

    def test_notes_state_doctor_authority(self):
        result = cs.run(ai_service=FakeAI(TestRunWithModel.REPLY),
                        aggregation=DISAGREEMENT, cards=[])
        self.assertTrue(any("医生" in n for n in result.notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
