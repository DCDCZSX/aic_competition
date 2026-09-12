"""依赖矩阵与智能追问的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_dependency_ask -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §6.1 依赖矩阵：缺失字段 ∈ 矩阵 且影响 ≥1 专科 → 高价值
- §10 追问：影响分诊/候选集的问题占比 ≥50%；≤1 轮 ≤3 问
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import dependency_matrix as dm  # noqa: E402
from services.ask_service import (  # noqa: E402
    MAX_QUESTIONS,
    apply_answers,
    build_questions,
    needs_reliability_downgrade,
    triage_related_ratio,
)
from services.department_domain import is_department  # noqa: E402


class TestMatrixIntegrity(unittest.TestCase):
    def test_matrix_size_within_spec(self):
        """规格书 §6.1：v1 手写 20-30 条核心映射。"""
        self.assertGreaterEqual(dm.matrix_size(), 20)
        self.assertLessEqual(dm.matrix_size(), 30)

    def test_all_departments_are_valid(self):
        """矩阵里出现的科室必须都在科室域内（防止写错名字）。"""
        for dep in dm.MATRIX:
            for name in dep.departments:
                with self.subTest(field=dep.field, department=name):
                    self.assertTrue(is_department(name), f"{dep.field} 引用了域外科室「{name}」")

    def test_field_keys_unique(self):
        keys = [d.field for d in dm.MATRIX]
        self.assertEqual(len(keys), len(set(keys)), "字段 key 重复")

    def test_triage_critical_fields_not_empty(self):
        self.assertTrue(dm.triage_critical_fields())

    def test_spec_examples_present(self):
        """规格书 §6.1 表格点名的字段必须存在。"""
        for field in ("blood_glucose_control", "urine_protein", "fundus_exam",
                      "cardiac_exam", "surgery_terms", "medication_list"):
            with self.subTest(field=field):
                self.assertIsNotNone(dm.depends_on(field), f"缺少规格书点名字段 {field}")


class TestMatrixQueries(unittest.TestCase):
    def test_departments_for_field(self):
        depts = dm.departments_for_field("blood_glucose_control")
        self.assertIn("内分泌科", depts)
        self.assertIn("眼科", depts)

    def test_fields_for_department(self):
        fields = dm.fields_for_department("眼科")
        self.assertIn("fundus_exam", fields)
        self.assertIn("blood_glucose_control", fields)

    def test_affected_departments_dedup(self):
        """增量更新影响范围：只返回受影响的科室，且不重复。"""
        depts = dm.affected_departments(["fundus_exam", "blood_glucose_control"])
        self.assertEqual(len(depts), len(set(depts)))
        self.assertIn("眼科", depts)
        self.assertIn("内分泌科", depts)

    def test_high_value_requires_department_overlap(self):
        # 眼底检查影响 眼科/内分泌科：在这些候选科室下算高价值
        self.assertTrue(dm.is_high_value("fundus_exam", ["眼科"]))
        # 换成完全不相关的科室组合，则不算
        self.assertFalse(dm.is_high_value("fundus_exam", ["骨科"]))

    def test_unknown_field_not_high_value(self):
        self.assertFalse(dm.is_high_value("外星人字段"))

    def test_high_value_gaps_matches_labels(self):
        gaps = ["血糖控制水平不详", "未提供眼底检查", "喜欢的颜色未知"]
        found = dm.high_value_gaps(gaps, ["内分泌科", "眼科"])
        self.assertIn("blood_glucose_control", found)
        self.assertIn("fundus_exam", found)
        self.assertNotIn("喜欢的颜色未知", found)


class TestBuildQuestions(unittest.TestCase):
    GAPS = ["血糖控制水平不详", "未提供眼底检查", "未提供肾功能结果"]

    def test_at_most_three_questions(self):
        qs = build_questions(gaps=self.GAPS, departments=["内分泌科", "肾内科", "眼科"])
        self.assertLessEqual(len(qs), MAX_QUESTIONS)

    def test_limit_zero_returns_empty(self):
        self.assertEqual(build_questions(gaps=self.GAPS, limit=0), [])

    def test_limit_cannot_exceed_max(self):
        qs = build_questions(gaps=self.GAPS, limit=99)
        self.assertLessEqual(len(qs), MAX_QUESTIONS)

    def test_each_question_has_why_and_affects(self):
        qs = build_questions(gaps=self.GAPS, departments=["内分泌科", "肾内科", "眼科"])
        self.assertTrue(qs)
        for q in qs:
            with self.subTest(field=q.field):
                self.assertTrue(q.text.strip(), "问题正文不能为空")
                self.assertIn("会影响", q.why)
                self.assertTrue(q.affects, "必须说明影响哪些判断")
                self.assertTrue(q.skippable, "必须可跳过")
                self.assertTrue(q.answerable_unknown, "必须可回答不知道")

    def test_triage_questions_first(self):
        """影响分诊的问题必须排在前面（保证占比验收线）。"""
        qs = build_questions(gaps=self.GAPS, departments=["内分泌科", "肾内科", "眼科"])
        flags = [q.affects_triage for q in qs]
        self.assertEqual(flags, sorted(flags, reverse=True), "分诊相关问题应优先")

    def test_triage_ratio_meets_threshold(self):
        """规格书 §10：影响分诊/候选集的问题占比 ≥50%。"""
        qs = build_questions(gaps=self.GAPS, departments=["内分泌科", "肾内科", "眼科"])
        self.assertGreaterEqual(triage_related_ratio(qs), 0.5)

    def test_non_high_value_gaps_ignored(self):
        qs = build_questions(gaps=["喜欢的颜色未知", "是否养宠物未知"],
                             departments=["内分泌科"])
        self.assertEqual(qs, [], "非矩阵字段不应被追问")

    def test_answered_fields_skipped(self):
        qs = build_questions(gaps=self.GAPS, departments=["内分泌科", "眼科"],
                             answered_fields=["fundus_exam"])
        self.assertNotIn("fundus_exam", [q.field for q in qs])

    def test_question_to_dict_shape(self):
        q = build_questions(gaps=self.GAPS, departments=["内分泌科"], limit=1)[0]
        data = q.to_dict()
        for key in ("field", "text", "why", "affects", "skippable",
                    "answerable_unknown", "options", "unknown_answer"):
            self.assertIn(key, data)


class TestApplyAnswers(unittest.TestCase):
    def test_answered_gap_resolved(self):
        result = apply_answers(
            gaps=["血糖控制水平不详", "未提供眼底检查"],
            answers={"blood_glucose_control": "空腹 7.8 左右"},
        )
        self.assertIn("blood_glucose_control", result["resolved_fields"])
        self.assertNotIn("血糖控制水平不详", result["remaining_gaps"])
        self.assertIn("未提供眼底检查", result["remaining_gaps"])

    def test_skipped_answer_keeps_gap_and_flags_unresolved(self):
        result = apply_answers(
            gaps=["血糖控制水平不详"],
            answers={"blood_glucose_control": "不清楚 / 跳过"},
        )
        self.assertIn("blood_glucose_control", result["unresolved_fields"])
        self.assertIn("血糖控制水平不详", result["remaining_gaps"])

    def test_unresolved_triggers_reliability_downgrade(self):
        self.assertTrue(needs_reliability_downgrade(["blood_glucose_control"]))
        self.assertFalse(needs_reliability_downgrade([]))

    def test_no_fabrication(self):
        """未回答的缺口不得凭空变成"已知"。"""
        result = apply_answers(gaps=["未提供眼底检查"], answers={})
        self.assertEqual(result["resolved_fields"], [])
        self.assertIn("未提供眼底检查", result["remaining_gaps"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
