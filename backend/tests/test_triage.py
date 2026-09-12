"""分诊决策链（S1 规则 + S3 编排）与就医路径的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_triage -v

验收对应《产品开发规格书-比赛版v-competition.md》：
- §4.3 决策链：strong 必选 / 模型新增需 conf≥0.65 / 冲突不静默 / 分数不进 UI
- §7.1 状态机与牵头方向
- §12 跨病种验收集（A/B/C/D 四类行为）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.case_flow import CaseFlow  # noqa: E402
from services.path_service import CONFIRM_PREFIX, build_path  # noqa: E402
from services.rule_engine import (  # noqa: E402
    BEHAVIOR_A,
    BEHAVIOR_B,
    BEHAVIOR_C,
    BEHAVIOR_D,
    audit_rules,
    evaluate,
    infer_behavior,
    rule_count,
    validate_rule_table,
)
from services.store import Store  # noqa: E402
from services.triage import MODEL_CONF_THRESHOLD, orchestrate  # noqa: E402

# 规格书 §12 验收集
CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往2型糖尿病5年，高血压10年。目前口服二甲双胍0.5g bid。"
          "检查：尿蛋白++，空腹血糖9.2。")
CASE_C = "患者，女性，35岁。反复发热3周，伴乏力、盗汗，血常规提示三系减少，诊断不明。"
CASE_D = "患者，男，45岁。房间隔缺损，拟行房间隔缺损修补术，合并心房颤动拟行射频消融，需术前评估。"
CASE_A = "患者，男，30岁。咽痛3天，轻度咳嗽，无发热。"


class TestRuleTable(unittest.TestCase):
    def test_rule_table_valid(self):
        self.assertEqual(validate_rule_table(), [], "规则表引用了域外科室或非法权重")

    def test_rule_count_reasonable(self):
        self.assertGreaterEqual(rule_count(), 40)

    def test_audit_list(self):
        items = audit_rules()
        self.assertEqual(len(items), rule_count())
        for item in items:
            self.assertTrue(item["signal"], "每条规则都要有可解释信号")
            self.assertTrue(item["keywords"], "每条规则都要有匹配条件")

    def test_eval0_missing_categories_present(self):
        """评测 v0 点名的四类漏科必须在规则表里有覆盖。"""
        text = "患者行冠脉搭桥手术，术后需康复训练，伴关节痛、抗核抗体阳性，头颅MRI示颅内占位"
        ev = evaluate(text)
        depts = ev.departments
        for expected in ("心胸外科", "康复医学科", "风湿免疫科", "神经外科"):
            with self.subTest(department=expected):
                self.assertIn(expected, depts, f"缺少 {expected} 的规则覆盖")

    def test_eval0_anesthesia_ultrasound(self):
        ev = evaluate("术前评估需麻醉科会诊，完善心超与彩超检查")
        self.assertIn("麻醉科", ev.departments)
        self.assertIn("超声科", ev.departments)

    def test_scores_and_signals(self):
        ev = evaluate(CASE_B)
        self.assertTrue(ev.scores)
        self.assertTrue(ev.signals)
        for dep, score in ev.scores.items():
            self.assertIn(score, (1, 2, 3), f"{dep} 分数异常")
        # 每个入选科室都要能解释"为什么"
        for dep in ev.departments:
            self.assertTrue(ev.signals.get(dep), f"{dep} 缺少命中信号")


class TestBehaviorType(unittest.TestCase):
    def test_case_a_single(self):
        self.assertEqual(infer_behavior(CASE_A, evaluate(CASE_A)), BEHAVIOR_A)

    def test_case_b_comorbid(self):
        self.assertEqual(infer_behavior(CASE_B, evaluate(CASE_B)), BEHAVIOR_B)

    def test_case_c_complex(self):
        self.assertEqual(infer_behavior(CASE_C, evaluate(CASE_C)), BEHAVIOR_C)

    def test_case_d_surgery(self):
        self.assertEqual(infer_behavior(CASE_D, evaluate(CASE_D)), BEHAVIOR_D)


class TestOrchestration(unittest.TestCase):
    def test_strong_rule_always_selected(self):
        """strong 命中必选，模型不能剔除。"""
        outcome = orchestrate(text=CASE_B)
        self.assertIn("肾内科", outcome.selected)  # 尿蛋白 → strong
        self.assertIn("内分泌科", outcome.selected)

    def test_primary_is_clinical_department(self):
        outcome = orchestrate(text=CASE_B)
        self.assertIsNotNone(outcome.primary)
        self.assertEqual(outcome.primary.role, "牵头")
        self.assertNotIn(outcome.primary.department, ("麻醉科", "影像科", "超声科", "检验科"))

    def test_case_b_primary_is_endocrinology(self):
        """规格书验收集 B：糖尿病共病应由内分泌牵头（而非并发症科室）。"""
        outcome = orchestrate(text=CASE_B)
        self.assertEqual(outcome.primary.department, "内分泌科", outcome.selected)

    def test_case_d_primary_is_surgeon(self):
        outcome = orchestrate(text=CASE_D)
        self.assertEqual(outcome.behavior_type, BEHAVIOR_D)
        self.assertEqual(outcome.primary.department, "心胸外科")

    def test_case_d_requires_periop_support(self):
        outcome = orchestrate(text=CASE_D)
        self.assertTrue(any(d in outcome.selected for d in ("麻醉科", "影像科", "超声科", "介入科")),
                        f"D 型应含围术期科室：{outcome.selected}")

    def test_max_departments(self):
        outcome = orchestrate(text=CASE_B + CASE_C + CASE_D)
        self.assertLessEqual(len(outcome.selected), 6)

    def test_model_new_department_requires_confidence(self):
        """仅模型新增：conf < 0.65 不入选，≥0.65 入选。"""
        low = orchestrate(text=CASE_A, model_picks=[{"department": "血液科", "confidence": 0.5}])
        self.assertNotIn("血液科", low.selected)

        high = orchestrate(text=CASE_A, model_picks=[{"department": "血液科", "confidence": 0.8,
                                                     "reason": "咽痛需排查血液系统"}])
        self.assertIn("血液科", high.selected)

    def test_model_reject_strong_is_recorded_not_applied(self):
        """冲突不静默：模型反驳 strong → 保留该科 + 记 conflict_log。"""
        outcome = orchestrate(text=CASE_B, model_picks=[
            {"department": "肾内科", "stance": "reject", "reason": "尿蛋白可能为误差"}])
        self.assertIn("肾内科", outcome.selected, "强规则命中不可被模型剔除")
        self.assertTrue(outcome.conflicts, "必须记录冲突")
        self.assertEqual(outcome.decision_path, "mixed")

    def test_out_of_domain_department_ignored(self):
        outcome = orchestrate(text=CASE_A, model_picks=[
            {"department": "口腔颌面外科", "confidence": 0.9}])
        self.assertNotIn("口腔颌面外科", outcome.selected)
        self.assertTrue(any("不在科室域内" in n for n in outcome.validator_notes))

    def test_scores_not_in_ui_payload(self):
        """分数不得出现在给前端的结构里（规格书 §4.3）。"""
        outcome = orchestrate(text=CASE_B)
        data = outcome.to_dict()
        for pick in [data["primary"]] + data["secondary"]:
            self.assertNotIn("score", pick)
            self.assertNotIn("confidence", pick)
        self.assertNotIn("score", str(data["reliability"]))

    def test_reliability_has_reasons(self):
        outcome = orchestrate(text=CASE_B)
        self.assertIsNotNone(outcome.reliability)
        self.assertTrue(outcome.reliability.reasons)

    def test_unresolved_gaps_downgrade_reliability(self):
        clean = orchestrate(text=CASE_B, completeness=0.9, unresolved_gaps=0)
        downgraded = orchestrate(text=CASE_B, completeness=0.9, unresolved_gaps=3)
        self.assertEqual(clean.reliability.level, "高")
        self.assertEqual(downgraded.reliability.level, "中")
        self.assertTrue(any("未确认" in r for r in downgraded.reliability.reasons))

    def test_no_departments_degrades(self):
        outcome = orchestrate(text="今天天气不错")
        self.assertEqual(outcome.decision_path, "degraded")
        self.assertEqual(outcome.selected, [])

    def test_needs_mdt_flag(self):
        self.assertFalse(orchestrate(text=CASE_A).needs_mdt)
        self.assertTrue(orchestrate(text=CASE_B).needs_mdt)
        self.assertTrue(orchestrate(text=CASE_D).needs_mdt)


class TestCarePath(unittest.TestCase):
    def setUp(self):
        self.outcome = orchestrate(text=CASE_B, case_id="C001")

    def test_path_has_prepare_and_visit(self):
        path = build_path(outcome=self.outcome, case_id="C001")
        kinds = [s.kind for s in path.steps]
        self.assertIn("prepare", kinds)
        self.assertIn("visit", kinds)

    def test_visit_step_uses_primary_department(self):
        path = build_path(outcome=self.outcome, case_id="C001")
        visit = [s for s in path.steps if s.kind == "visit"][0]
        self.assertIn(self.outcome.primary.department, visit.title)

    def test_path_is_conditional_not_imperative(self):
        """路径必须表达为条件分支建议，且带"以医生为准"提示。"""
        path = build_path(outcome=self.outcome, case_id="C001")
        self.assertTrue(any(s.condition for s in path.steps), "缺少条件分支步骤")
        self.assertTrue(any("以线下医生判断为准" in n for n in path.notes))

    def test_tasks_generated(self):
        path = build_path(outcome=self.outcome, case_id="C001")
        self.assertTrue(path.tasks)
        for t in path.tasks:
            t.validate()  # 指令式文案会被 schemas 拒绝

    def test_medical_tasks_are_confirm_style(self):
        path = build_path(
            outcome=self.outcome, case_id="C001",
            checks_by_department={"眼科": ["眼底照相"], "肾内科": ["肾功能复查"]},
        )
        consult = [t for t in path.tasks if t.type == "就医咨询"]
        self.assertTrue(consult)
        for t in consult:
            self.assertTrue(t.content.startswith(CONFIRM_PREFIX),
                            f"医疗类任务必须是确认式：{t.content}")

    def test_tasks_types_within_enum(self):
        from schemas import TASK_TYPES
        path = build_path(outcome=self.outcome, case_id="C001")
        for t in path.tasks:
            self.assertIn(t.type, TASK_TYPES)

    def test_empty_outcome_gives_note(self):
        empty = orchestrate(text="今天天气不错")
        path = build_path(outcome=empty, case_id="C000")
        self.assertEqual(path.steps, [])
        self.assertTrue(path.notes)


class TestCaseFlowIntegration(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"))

    def tearDown(self):
        self.flow.store.close()

    def test_create_case_returns_triage_and_path(self):
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科",
                                  goals=["multi_disease"], age=58)
        self.assertTrue(r.ok, r.errors)
        self.assertIsNotNone(r.triage)
        self.assertIsNotNone(r.path)
        self.assertEqual(r.triage["behavior_type"], BEHAVIOR_B)
        self.assertEqual(r.triage["primary"]["department"], "内分泌科")
        self.assertTrue(r.path["steps"])
        self.assertTrue(r.path["tasks"])

    def test_case_state_awaiting_doctor(self):
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科")
        self.assertEqual(r.case_state, "awaiting_doctor")

    def test_transitions_are_legal(self):
        """状态流转必须走合法链路（pending → analyzing → awaiting_doctor）。"""
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科")
        rows = self.flow.store.list_transitions(r.case_id)
        self.assertEqual([(x["from_state"], x["to_state"]) for x in rows],
                         [("pending_materials", "analyzing"), ("analyzing", "awaiting_doctor")])

    def test_tasks_persisted(self):
        r = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科")
        case = self.flow.store.load_case(r.case_id)
        self.assertTrue(case["payload"]["tasks"])
        self.assertIsNotNone(case["payload"]["triage"])

    def test_crisis_still_blocked(self):
        r = self.flow.create_case(medical_history="患者持续胸痛伴大汗")
        self.assertEqual(r.blocked_by, "G1")
        self.assertIsNone(r.triage)


if __name__ == "__main__":
    unittest.main(verbosity=2)
