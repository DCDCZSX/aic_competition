"""病例画像抽取器的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_portrait_extractor -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §5.1 画像 schema（含 source_refs / completeness / direct_quote_coverage）
- §4.4 G4：原文直引覆盖率 <0.60 → 阻断
- 主文档 §5.4：只抽实际存在的信息，不编造
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.portrait_extractor import extract  # noqa: E402

# 一段贴近真实的病例文本（含年龄/性别/症状/病程/既往史/用药/检查）
SAMPLE = (
    "患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。\n"
    "既往2型糖尿病5年，高血压10年，血糖控制情况不详。\n"
    "目前口服二甲双胍0.5g bid，苯磺酸氨氯地平5mg qd。\n"
    "检查：尿蛋白++，空腹血糖9.2，血红蛋白120。\n"
    "曾行心电图检查提示异常，未做眼底检查。"
)

THIN_SAMPLE = "患者，男，60岁。有点不舒服。"


class TestRuleExtraction(unittest.TestCase):
    def setUp(self):
        self.result = extract(SAMPLE, case_id="C001", main_issue="该看哪个科")
        self.profile = self.result.profile

    def test_basic_fields(self):
        self.assertEqual(self.profile.age, 58)
        self.assertEqual(self.profile.sex, "男")
        self.assertIn("视物模糊", self.profile.chief_complaint)

    def test_symptoms_with_onset(self):
        names = [s.symptom for s in self.profile.symptoms_timeline]
        self.assertTrue(any("视物模糊" in n for n in names), names)
        self.assertTrue(any("浮肿" in n for n in names), names)
        for s in self.profile.symptoms_timeline:
            self.assertTrue(s.onset, f"{s.symptom} 缺少起病时长")
            self.assertTrue(s.source_refs, f"{s.symptom} 缺少 source_refs")

    def test_diseases(self):
        names = [d.name for d in self.profile.diseases]
        self.assertIn("2型糖尿病", names)
        self.assertIn("高血压", names)
        dm = [d for d in self.profile.diseases if d.name == "2型糖尿病"][0]
        self.assertIn("5年", dm.duration)
        self.assertTrue(dm.source_refs)

    def test_medications(self):
        names = [m.name for m in self.profile.medications]
        self.assertTrue(any("二甲双胍" in n for n in names), names)

    def test_exams(self):
        items = [e.item for e in self.profile.exams]
        self.assertTrue(any("尿蛋白" in i for i in items), items)

    def test_information_gaps_detected(self):
        gaps = self.profile.information_gaps
        self.assertTrue(any("眼底" in g for g in gaps), gaps)
        self.assertTrue(any("过敏" in g for g in gaps), gaps)

    def test_negated_exam_counts_as_gap(self):
        """文本写"未做眼底检查" → 应识别为缺口（否定语境）。"""
        result = extract("患者男，58岁。主诉：视物模糊3月。未做眼底检查。", case_id="C010")
        self.assertTrue(any("眼底" in g for g in result.profile.information_gaps),
                        result.profile.information_gaps)

    def test_negative_result_counts_as_provided(self):
        """文本写"眼底检查未见异常" → 已提供资料，不应进缺口。"""
        result = extract("患者男，58岁。主诉：视物模糊3月。眼底检查未见异常。", case_id="C011")
        self.assertFalse(any("眼底" in g for g in result.profile.information_gaps),
                         result.profile.information_gaps)

    def test_source_refs_format(self):
        for d in self.profile.diseases:
            self.assertTrue(d.source_refs[0].startswith("S-1:"), d.source_refs)

    def test_method_is_rule_without_llm(self):
        self.assertEqual(self.result.method, "rule")


class TestGate(unittest.TestCase):
    def test_rich_text_passes_gate(self):
        result = extract(SAMPLE, case_id="C001", main_issue="该看哪个科")
        self.assertGreaterEqual(result.direct_quote_coverage, 0.60)
        self.assertTrue(result.passes_gate, result.warnings)

    def test_thin_text_blocks_or_warns(self):
        """信息很少的文本：覆盖率低 → G4 阻断提示。"""
        result = extract(THIN_SAMPLE, case_id="C002", main_issue="")
        if result.direct_quote_coverage < 0.60:
            self.assertFalse(result.passes_gate)
            self.assertTrue(any("G4" in w for w in result.warnings), result.warnings)

    def test_completeness_is_ratio(self):
        result = extract(SAMPLE, case_id="C001", main_issue="该看哪个科")
        self.assertGreaterEqual(result.profile.completeness, 0.60)
        self.assertLessEqual(result.profile.completeness, 1.0)

    def test_gate_threshold_constant(self):
        from schemas import PatientProfile
        self.assertEqual(PatientProfile.MIN_DIRECT_QUOTE_COVERAGE, 0.60)


class TestNoFabrication(unittest.TestCase):
    """红线：没写的信息不得被编造出来。"""

    def test_absent_fields_stay_empty(self):
        result = extract("患者，男，45岁。主诉：咳嗽2周。", case_id="C003")
        p = result.profile
        self.assertEqual(p.diseases, [], "文本未提既往病，不应凭空生成")
        self.assertEqual(p.medications, [], "文本未提用药，不应凭空生成")
        self.assertTrue(p.information_gaps, "缺失信息应进入缺口列表")

    def test_empty_text_is_safe(self):
        result = extract("", case_id="C004")
        self.assertEqual(result.profile.diseases, [])
        self.assertEqual(result.profile.age, None)
        self.assertFalse(result.passes_gate)

    def test_schema_validation_runs(self):
        result = extract(SAMPLE, case_id="C001")
        self.assertEqual(result.schema_error, "", f"画像应通过 schema 校验：{result.schema_error}")


class TestLLMFallback(unittest.TestCase):
    class _BrokenService:
        def call_ai_api(self, prompt, role_description=None):
            raise RuntimeError("网络不可用")

    class _BadJSONService:
        def call_ai_api(self, prompt, role_description=None):
            return "这不是 JSON"

    def test_llm_failure_falls_back_to_rules(self):
        result = extract(SAMPLE, case_id="C001", use_llm=True,
                         ai_service=self._BrokenService())
        self.assertEqual(result.method, "rule")
        self.assertTrue(any("模型增强失败" in w for w in result.warnings))
        self.assertIn("2型糖尿病", [d.name for d in result.profile.diseases])

    def test_llm_bad_json_falls_back(self):
        result = extract(SAMPLE, case_id="C001", use_llm=True,
                         ai_service=self._BadJSONService())
        self.assertEqual(result.method, "rule")
        self.assertIn("2型糖尿病", [d.name for d in result.profile.diseases])

    def test_unexpected_llm_exception_detail_is_not_returned(self):
        secret = "portrait-secret-detail"
        with mock.patch("services.portrait_extractor._extract_with_llm",
                        side_effect=RuntimeError(secret)):
            result = extract(SAMPLE, case_id="C001", use_llm=True,
                             ai_service=object())
        self.assertEqual(result.method, "rule")
        self.assertNotIn(secret, " ".join(result.warnings))

    def test_llm_missing_service_is_safe(self):
        result = extract(SAMPLE, case_id="C001", use_llm=True, ai_service=None)
        self.assertEqual(result.method, "rule")
        self.assertTrue(result.profile.diseases)


if __name__ == "__main__":
    unittest.main(verbosity=2)
