"""任务状态机（3.4）与就诊后更新/变化摘要（3.6）的单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_update_flow -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import task_service as ts  # noqa: E402
from services import update_service as us  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402

CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。")


def make_tasks():
    return [
        ts.create_task("C001", 1, "资料准备", "整理血糖记录", "high", "path:内分泌科"),
        ts.create_task("C001", 2, "就医咨询", "向医生确认是否需要：完善眼底检查", "high", "眼科:checks"),
        ts.create_task("C001", 3, "就诊后资料更新", "就诊后上传新检查结果", "medium", "path:followup"),
    ]


class TestTaskStateMachine(unittest.TestCase):
    def setUp(self):
        self.tasks = make_tasks()

    def test_happy_path(self):
        u1 = ts.update_state(self.tasks, "T-001", "doing", actor="patient")
        self.assertEqual(u1.from_state, "todo")
        self.assertEqual(u1.to_state, "doing")
        u2 = ts.update_state(self.tasks, "T-001", "done")
        self.assertEqual(u2.to_state, "done")
        self.assertEqual(self.tasks[0]["state"], "done")

    def test_illegal_transition_rejected(self):
        ts.update_state(self.tasks, "T-001", "done")
        with self.assertRaises(ts.TaskError):
            ts.update_state(self.tasks, "T-001", "doing")   # 已完成 → 进行中 非法

    def test_done_can_be_reopened(self):
        ts.update_state(self.tasks, "T-001", "done")
        u = ts.update_state(self.tasks, "T-001", "reopened", reason="新资料使结论变化")
        self.assertEqual(u.to_state, "reopened")

    def test_cancel_and_reactivate(self):
        ts.update_state(self.tasks, "T-001", "cancelled")
        self.assertEqual(self.tasks[0]["state"], "cancelled")
        ts.update_state(self.tasks, "T-001", "todo")
        self.assertEqual(self.tasks[0]["state"], "todo")

    def test_unknown_state_rejected(self):
        with self.assertRaises(ts.TaskError):
            ts.update_state(self.tasks, "T-001", "flying")

    def test_unknown_task_rejected(self):
        with self.assertRaises(ts.TaskError):
            ts.update_state(self.tasks, "T-999", "doing")

    def test_history_recorded(self):
        ts.update_state(self.tasks, "T-001", "doing", actor="patient", reason="开始整理")
        history = self.tasks[0]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["to"], "doing")
        self.assertTrue(history[0]["at"])

    def test_recompute_trigger_only_for_result_tasks(self):
        """仅"就诊后资料更新"类任务完成才触发重算。"""
        u_prep = ts.update_state(self.tasks, "T-001", "done")
        self.assertFalse(u_prep.trigger_recompute)
        u_update = ts.update_state(self.tasks, "T-003", "done")
        self.assertTrue(u_update.trigger_recompute)

    def test_summary(self):
        ts.update_state(self.tasks, "T-001", "done")
        s = ts.summarize(self.tasks)
        self.assertEqual(s["total"], 3)
        self.assertEqual(s["done"], 1)
        self.assertEqual(s["open"], 2)
        self.assertAlmostEqual(s["progress"], 1 / 3, places=2)

    def test_validate(self):
        self.assertEqual(ts.validate_tasks(self.tasks), [])
        bad = [{"task_id": "X", "type": "随便", "state": "unknown"}]
        self.assertEqual(len(ts.validate_tasks(bad)), 2)

    def test_all_states_in_transition_table(self):
        from schemas import TASK_STATES
        for state in TASK_STATES:
            self.assertIn(state, ts.TRANSITIONS, f"{state} 缺少转移规则")


class TestUpdateService(unittest.TestCase):
    def test_affected_departments_by_matrix(self):
        result = us.determine_affected("检查结果", "眼底检查：II期病变", ["内分泌科", "眼科", "肾内科"])
        self.assertIn("眼科", result["departments"])
        self.assertTrue(result["modules"])

    def test_affected_only_among_current(self):
        """只保留当前参与科室（矩阵命中的其他科室不算本次受影响）。"""
        only_eye = us.determine_affected("检查结果", "眼底检查", ["眼科"])
        self.assertEqual(only_eye["departments"], ["眼科"])
        full = us.determine_affected("检查结果", "眼底检查", ["眼科", "内分泌科", "肾内科", "骨科"])
        self.assertGreater(len(full["departments"]), 1, "检查结果类更新应波及多个参与科室")
        self.assertTrue(set(full["departments"]) <= {"眼科", "内分泌科", "肾内科", "骨科"})

    def test_check_result_uses_content_specific_dependencies(self):
        """眼底资料只影响矩阵中真正依赖眼底的当前科室。"""
        affected = us.determine_affected(
            "检查结果", "眼底检查：视网膜病变", ["眼科", "内分泌科", "肾内科"])
        self.assertEqual(affected["departments"], ["眼科", "内分泌科"])

    def test_gaps_filled_by_keywords(self):
        filled = us.gaps_filled_by("检查结果：新增眼底检查，视网膜可见病变")
        self.assertIn("眼底检查", filled)

    def test_gaps_filled_by_negative(self):
        self.assertEqual(us.gaps_filled_by("今天天气不错"), [])

    def test_validate_update(self):
        with self.assertRaises(ValueError):
            us.validate_update("随便", "内容", us.SOURCE_PATIENT)
        with self.assertRaises(ValueError):
            us.validate_update("检查结果", "   ", us.SOURCE_PATIENT)
        with self.assertRaises(ValueError):
            us.validate_update("检查结果", "内容", "外星来源")

    def test_doctor_opinion_source(self):
        opinion = us.build_doctor_opinion("眼科", "建议眼底激光", us.SOURCE_DOCTOR)
        self.assertTrue(opinion["is_verified"])
        patient_view = us.build_doctor_opinion("眼科", "医生说没事", us.SOURCE_PATIENT)
        self.assertFalse(patient_view["is_verified"], "患者转述不应视为已验证")

    def test_change_summary_detects_department_change(self):
        summary = us.build_change_summary(
            case_id="C001",
            old_triage={"primary": {"department": "内分泌科"}, "selected": ["内分泌科"],
                        "reliability": {"level": "中"}},
            new_triage={"primary": {"department": "眼科"}, "selected": ["内分泌科", "眼科"],
                        "reliability": {"level": "高"}},
            old_path={"steps": [{"step_id": "S1"}], "tasks": []},
            new_path={"steps": [{"step_id": "S1"}, {"step_id": "S2"}], "tasks": []},
            updates=[])
        self.assertTrue(summary.changed)
        self.assertTrue(any("牵头方向" in c for c in summary.changed))
        self.assertTrue(any("可靠性" in c for c in summary.changed))
        self.assertTrue(summary.needs_doctor_review)
        self.assertTrue(summary.has_changes)

    def test_change_summary_no_probability(self):
        """红线：变化摘要不得出现概率数字。"""
        import json
        summary = us.build_change_summary(
            case_id="C001", old_triage={}, new_triage={}, old_path={}, new_path={}, updates=[])
        text = json.dumps(summary.to_dict(), ensure_ascii=False)
        for token in ("%", "概率", "置信度"):
            self.assertNotIn(token, text)


class TestUpdateFlowEndToEnd(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"))
        self.case = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科", age=58)

    def tearDown(self):
        self.flow.store.close()

    def test_add_update_fills_gap(self):
        before = len(self.case.gaps)
        result = self.flow.add_update(self.case.case_id, kind="检查结果",
                                      content="新增眼底检查：双眼糖尿病视网膜病变II期")
        self.assertTrue(result["ok"])
        case = self.flow.get_case(self.case.case_id)
        after = case["payload"]["gaps"]
        self.assertLess(len(after), before, "新资料应填补对应缺口")
        self.assertFalse(any("眼底检查" in g for g in after))

    def test_update_records_affected_modules(self):
        result = self.flow.add_update(self.case.case_id, kind="检查结果",
                                      content="眼底检查：视网膜病变")
        self.assertTrue(result["affected"]["departments"])
        self.assertTrue(result["affected"]["modules"])

    def test_update_before_confirmation_stays_awaiting_doctor(self):
        self.flow.add_update(self.case.case_id, kind="检查结果", content="眼底检查正常")
        case = self.flow.get_case(self.case.case_id)
        self.assertEqual(case["case_state"], "awaiting_doctor")

    def test_update_after_confirmation_becomes_awaiting_recheck(self):
        self.flow.store.transition_case(self.case.case_id, "doctor_confirmed", actor="doctor")
        self.flow.add_update(self.case.case_id, kind="检查结果", content="眼底检查正常")
        case = self.flow.get_case(self.case.case_id)
        self.assertEqual(case["case_state"], "awaiting_recheck")

    def test_update_audited_and_transitioned(self):
        self.flow.add_update(self.case.case_id, kind="症状变化", content="浮肿较前减轻")
        events = [a["event"] for a in self.flow.store.list_audit(self.case.case_id)]
        self.assertIn("case_updated", events)
        transitions = [(t["from_state"], t["to_state"])
                       for t in self.flow.store.list_transitions(self.case.case_id)]
        self.assertNotIn(("awaiting_doctor", "awaiting_recheck"), transitions)

    def test_doctor_opinion_not_from_patient(self):
        """红线：患者转述的"医生说……"不得被当作医生确认意见。"""
        self.flow.add_update(self.case.case_id, kind="医生意见", content="医生说问题不大",
                             source="患者上传", department="眼科")
        case = self.flow.get_case(self.case.case_id)
        opinions = case["payload"].get("doctor_opinions") or []
        self.assertTrue(opinions, "医生意见应被记录（但标记为待验证）")
        for op in opinions:
            self.assertFalse(op["is_verified"], "患者转述不得视为已验证的医生意见")
            self.assertEqual(op["source"], us.SOURCE_PATIENT)

    def test_patient_transcribed_opinion_not_counted_as_agreement(self):
        """患者转述的意见不能进入"AI 与医生一致"列表。"""
        result = self.flow.add_update(self.case.case_id, kind="医生意见",
                                      content="医生也说该看眼科",
                                      source="患者上传", department="眼科")
        cmp = result["change_summary"]["doctor_opinion_comparison"]
        self.assertNotIn("眼科", cmp["agree"])
        self.assertTrue(cmp["unverified"])
        self.assertTrue(any("尚未经医生端确认" in n
                            for n in result["change_summary"]["needs_doctor_review"]))

    def test_doctor_opinion_comparison(self):
        result = self.flow.add_update(self.case.case_id, kind="医生意见",
                                      content="建议眼底激光治疗",
                                      source="医生上传", department="眼科")
        cmp = result["change_summary"]["doctor_opinion_comparison"]
        self.assertIn("眼科", cmp["agree"] + [d.get("department") for d in cmp["differences"]])

    def test_invalid_kind_rejected(self):
        result = self.flow.add_update(self.case.case_id, kind="随便", content="内容")
        self.assertFalse(result["ok"])

    def test_task_state_end_to_end(self):
        task_id = self.case.path["tasks"][0]["task_id"]
        ok = self.flow.update_task_state(self.case.case_id, task_id, "doing")
        self.assertTrue(ok["ok"])
        bad = self.flow.update_task_state(self.case.case_id, task_id, "reopened")  # doing→reopened 非法
        self.assertFalse(bad["ok"])

    def test_get_change_summary(self):
        self.flow.add_update(self.case.case_id, kind="检查结果", content="眼底检查正常")
        data = self.flow.get_change_summary(self.case.case_id)
        self.assertIsNotNone(data)
        self.assertTrue(data["updates"])

    def test_update_reopens_only_affected_review_items(self):
        """眼底资料更新后只重开眼科/内分泌科决定，旧决定仍留在历史。"""
        review = self.flow.get_review(self.case.case_id)
        department_items = {
            item["target_id"]: item for item in review["items"]
            if item["target_type"] == "department_selection"
        }
        for department in ("眼科", "内分泌科", "肾内科"):
            item = department_items.get(department)
            if item:
                result = self.flow.submit_decision(
                    self.case.case_id, item["item_id"], "accept", reviewer_name="医生")
                self.assertTrue(result["ok"], result.get("error"))

        result = self.flow.add_update(
            self.case.case_id, kind="检查结果", content="眼底检查：视网膜病变")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["affected"]["departments"], ["眼科", "内分泌科"])
        self.assertEqual(result["affected"]["affected_departments"], ["眼科", "内分泌科"])
        self.assertIn("reopened_item_ids", result["change_summary"])
        self.assertTrue(any("眼科" in label
                            for label in result["change_summary"].get("reopened_item_labels", [])))

        refreshed = self.flow.get_review(self.case.case_id)
        by_department = {
            item["target_id"]: item for item in refreshed["items"]
            if item["target_type"] == "department_selection"
        }
        for department in ("眼科", "内分泌科"):
            self.assertEqual(by_department[department]["state"], "awaiting")
            self.assertEqual(by_department[department]["content_revision"], 2)
        if "肾内科" in by_department:
            self.assertEqual(by_department["肾内科"]["state"], "confirmed")
            self.assertEqual(by_department["肾内科"]["content_revision"], 1)

        stale = refreshed["stale_decisions"]
        self.assertTrue(stale)
        self.assertTrue(all(int(item.get("content_revision") or 1) == 1 for item in stale))

        history = self.flow.get_review_history(self.case.case_id)
        self.assertTrue(history["decisions"])
        self.assertTrue(all("content_revision" in item
                            and int(item["content_revision"]) == 1
                            for item in history["decisions"]))

    def test_refresh_mdt_only_replaces_targets_and_adds_new_department(self):
        """局部 MDT 更新只替换目标卡，并为新入选科室建立待分析卡。"""
        payload = {
            "profile": {"chief_complaint": "视物模糊"},
            "answers": {},
            "mdt": {
                "departments": ["眼科", "肾内科"],
                "analyses": [
                    {"department": "眼科", "status": "ok", "facts": ["旧眼科资料"]},
                    {"department": "肾内科", "status": "ok", "facts": ["保留肾科资料"]},
                ],
                "aggregation": {"consensus": [], "disagreements": [],
                                "uncertainties": [], "open_questions": []},
            },
        }
        updated, refreshed = self.flow._refresh_affected_mdt(
            payload=payload, text="新增眼底检查", departments=["眼科", "内分泌科"],
            case_id=self.case.case_id,
        )
        self.assertEqual(refreshed, ["眼科", "内分泌科"])
        analyses = {item["department"]: item for item in updated["analyses"]}
        self.assertEqual(analyses["眼科"]["status"], "stale")
        self.assertEqual(analyses["肾内科"]["facts"], ["保留肾科资料"])
        self.assertEqual(analyses["内分泌科"]["status"], "stale")


if __name__ == "__main__":
    unittest.main(verbosity=2)
