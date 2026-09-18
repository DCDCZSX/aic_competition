"""医生端审核台（阶段 4.1-4.5）单元测试。

运行（在 CHAIS/backend 目录下）：
    python -m unittest tests.test_review -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import review_service as rs  # noqa: E402
from services.case_flow import CaseFlow  # noqa: E402
from services.store import Store  # noqa: E402

CASE_B = ("患者：张*，男，58岁。主诉：视物模糊3月，双下肢浮肿1周。"
          "既往2型糖尿病5年，高血压10年。检查：尿蛋白++，空腹血糖9.2。")


def sample_case():
    """构造一份含分诊/路径/MDT 聚合的病例（不调用 LLM）。"""
    return {
        "case_id": "C0001",
        "case_state": "awaiting_doctor",
        "report_version": 1,
        "main_issue": "该看哪个科",
        "payload": {
            "profile": {"chief_complaint": "视物模糊3月", "age": 58, "sex": "男",
                        "diseases": [], "exams": [], "medications": []},
            "triage": {
                "behavior_type": "B",
                "primary": {"department": "内分泌科", "role": "牵头",
                            "signals": ["糖尿病5年"], "reason": "规则命中糖尿病"},
                "secondary": [{"department": "肾内科", "role": "协同", "signals": ["尿蛋白++"]},
                              {"department": "眼科", "role": "协同", "signals": ["视物模糊"]}],
                "reliability": {"level": "中", "reasons": ["仍有信息缺口"]},
                "selected": ["内分泌科", "肾内科", "眼科"],
            },
            "path": {"steps": [
                {"step_id": "S1", "title": "整理近 3 个月血糖记录", "detail": "就诊前准备"},
                {"step_id": "S2", "title": "向医生确认是否需要完善眼底检查",
                 "detail": "医疗行为需医生确认", "requires_doctor": True},
            ], "tasks": []},
            "mdt": {"aggregation": {
                "consensus": [{"topic": "眼底检查", "supporters": ["眼科", "内分泌科"],
                               "example": "建议向医生确认是否需要眼底检查"}],
                "disagreements": [{"topic": "先查眼底 vs 先查肾功",
                                   "positions": {"眼科": "先查眼底", "肾内科": "先查肾功"},
                                   "resolving_info": "需补充检查依据"}],
                "open_questions": ["是否需要眼科随诊", "分歧待裁决：先查眼底 vs 先查肾功"],
                "uncertainties": [],
            }},
            "gaps": ["眼底检查：信息不足"],
            "tasks": [],
        },
    }


class TestBuildItems(unittest.TestCase):
    def setUp(self):
        self.items = rs.build_items(sample_case())
        self.by_type = {}
        for item in self.items:
            self.by_type.setdefault(item.target_type, []).append(item)

    def test_all_four_target_types_present(self):
        for target in ("department_selection", "path_step", "consensus_item", "patient_advice"):
            self.assertIn(target, self.by_type, f"缺少 {target} 条目")

    def test_department_items_include_lead(self):
        titles = [i.title for i in self.by_type["department_selection"]]
        self.assertTrue(any("牵头 内分泌科" in t for t in titles), titles)
        self.assertEqual(len(self.by_type["department_selection"]), 3)

    def test_lead_department_first(self):
        self.assertEqual(self.by_type["department_selection"][0].target_id, "内分泌科")

    def test_item_ids_stable(self):
        again = rs.build_items(sample_case())
        self.assertEqual([i.item_id for i in self.items], [i.item_id for i in again])

    def test_item_id_format(self):
        for item in self.items:
            self.assertEqual(item.item_id, f"{item.target_type}:{item.target_id}")

    def test_disagreement_becomes_item(self):
        texts = [i.title for i in self.by_type["consensus_item"]]
        self.assertTrue(any("分歧待裁决" in t for t in texts), texts)

    def test_open_question_not_duplicated(self):
        contents = [i.ai_content for i in self.items if i.target_type == "patient_advice"]
        self.assertNotIn("分歧待裁决：先查眼底 vs 先查肾功", contents)

    def test_no_probability_numbers(self):
        """红线：条目内容不得出现概率/置信度数字。"""
        text = json.dumps([i.to_dict() for i in self.items], ensure_ascii=False)
        for token in ("%", "概率", "置信度", "confidence"):
            self.assertNotIn(token, text)

    def test_clean_strips_confidence_words(self):
        item = rs.ReviewItem(item_id="x:1", target_type="path_step", target_id="1",
                             title="t", ai_content="置信度 0.8 支持该判断")
        self.assertNotIn("0.8", item.to_dict()["ai_content"])

    def test_clean_keeps_lab_values(self):
        """红线只针对概率表述：真实检验数值与剂量不得被改写。"""
        item = rs.ReviewItem(item_id="x:2", target_type="path_step", target_id="2",
                             title="t", ai_content="空腹血糖9.2mmol/L，二甲双胍0.5g bid")
        out = item.to_dict()["ai_content"]
        self.assertIn("9.2", out)
        self.assertIn("0.5g", out)

    def test_empty_case_yields_no_items(self):
        self.assertEqual(rs.build_items({"case_id": "C0", "payload": {}}), [])


class TestDecisions(unittest.TestCase):
    def setUp(self):
        self.items = rs.build_items(sample_case())
        self.item = self.items[0]

    def test_accept(self):
        d = rs.make_decision(case_id="C0001", item=self.item, action="accept",
                             reviewer_name="医学生")
        self.assertEqual(d.action, "accept")
        self.assertEqual(d.before, self.item.ai_content)
        self.assertEqual(d.reviewer.role, "医学生")

    def test_modify_requires_reason(self):
        with self.assertRaises(rs.ReviewError):
            rs.make_decision(case_id="C0001", item=self.item, action="modify",
                             after="改为先看眼科")

    def test_modify_requires_after(self):
        with self.assertRaises(rs.ReviewError):
            rs.make_decision(case_id="C0001", item=self.item, action="modify",
                             reason="依据不足")

    def test_modify_ok(self):
        d = rs.make_decision(case_id="C0001", item=self.item, action="modify",
                             after="改为先看眼科", reason="补充眼科证据")
        self.assertEqual(d.after, "改为先看眼科")

    def test_reject_requires_reason(self):
        with self.assertRaises(rs.ReviewError):
            rs.make_decision(case_id="C0001", item=self.item, action="reject")
        d = rs.make_decision(case_id="C0001", item=self.item, action="reject",
                             reason="与既往意见不符")
        self.assertEqual(d.action, "reject")

    def test_unknown_action(self):
        with self.assertRaises(rs.ReviewError):
            rs.make_decision(case_id="C0001", item=self.item, action="瞎操作")

    def test_apply_decisions_states(self):
        items = rs.build_items(sample_case())
        decisions = [
            {"target_type": "department_selection", "target_id": "内分泌科",
             "action": "accept", "reason": "", "after": None, "created_at": "2026-09-12",
             "reviewer": {"role": "医学生"}},
            {"target_type": "department_selection", "target_id": "眼科",
             "action": "modify", "after": "改为眼科牵头", "reason": "证据更强",
             "created_at": "2026-09-12", "reviewer": {"role": "医学生"}},
        ]
        applied = {i.target_id: i for i in rs.apply_decisions(items, decisions)}
        self.assertEqual(applied["内分泌科"].state, rs.CONFIRMED)
        self.assertEqual(applied["眼科"].state, rs.MODIFIED)
        self.assertEqual(applied["眼科"].final_content, "改为眼科牵头")
        self.assertTrue(applied["眼科"].changed_by_doctor)
        self.assertEqual(applied["肾内科"].state, rs.AWAITING)

    def test_latest_decision_wins(self):
        items = rs.build_items(sample_case())
        decisions = [
            {"target_type": "department_selection", "target_id": "眼科",
             "action": "reject", "reason": "先否", "created_at": "2"},
            {"target_type": "department_selection", "target_id": "眼科",
             "action": "accept", "reason": "复核后接受", "created_at": "3"},
        ]
        applied = {i.target_id: i for i in rs.apply_decisions(items, decisions)}
        self.assertEqual(applied["眼科"].state, rs.CONFIRMED)

    def test_summary_progress(self):
        # 免审态由 apply_decisions 派生（build_items 只给出 needs_doctor_review 语义）
        items = rs.apply_decisions(rs.build_items(sample_case()), [])
        exempt = [i for i in items if i.state == rs.AUTO_PROCESSED]
        self.assertTrue(exempt, "样例病例应有免审的路径步骤")
        s0 = rs.summarize(items)
        # 待审只算"必审"条目：未要求医生决定的路径步骤不计入待审
        self.assertEqual(s0["pending"], s0["total"] - len(exempt))
        self.assertEqual(s0["auto_processed"], len(exempt))
        self.assertFalse(s0["can_publish"])
        for item in items:
            item.state = rs.CONFIRMED
        s1 = rs.summarize(items)
        self.assertTrue(s1["can_publish"])
        self.assertEqual(s1["progress"], 1.0)

    def test_pending_questions_only_uncertain(self):
        items = rs.build_items(sample_case())
        items[0].state = rs.UNCERTAIN
        items[0].decision_reason = "证据不足"
        items[1].state = rs.CONFIRMED
        questions = rs.pending_questions(items)
        self.assertEqual(len(questions), 1)
        self.assertIn(items[0].title, questions[0])
        self.assertIn("证据不足", questions[0])

    def test_find_item_missing(self):
        with self.assertRaises(rs.ReviewError):
            rs.find_item(self.items, "no_such_item")

    # --------- 2026-09-17 修的真缺陷：免审条目不能计入待审、不能阻塞发布 ---------
    def test_exempt_items_do_not_block_publishing(self):
        """规则判定免审的条目：不计入待审，也不阻塞发布（修复前它们会把发布卡死）。"""
        items = rs.apply_decisions(rs.build_items(sample_case()), [])
        exempt = [i for i in items if i.state == rs.AUTO_PROCESSED]
        self.assertTrue(exempt)
        summary = rs.summarize(items)
        self.assertEqual(summary["pending"], summary["total"] - len(exempt))
        self.assertEqual(summary["auto_processed"], len(exempt))
        # 免审条目既不属于"医生已处理"，也不出现在阻塞原因里
        self.assertEqual(summary["doctor_reviewed"], 0)
        self.assertNotIn("免审", "；".join(summary["blocking_reasons"]))
        for item in items:
            if item.needs_doctor_review:
                item.state = rs.CONFIRMED
                item.decision_id = f"d-{item.item_id}"
        done = rs.summarize(items)
        self.assertTrue(done["can_publish"], done["blocking_reasons"])
        self.assertEqual(done["pending"], 0)
        self.assertEqual(done["auto_processed"], len(exempt))

    def test_required_step_still_blocks_publishing(self):
        """要求医生决定的路径步骤（requires_doctor）仍然必须审 —— 不能借"免审"漏掉。"""
        items = rs.apply_decisions(rs.build_items(sample_case()), [])
        required_steps = [i for i in items
                          if i.target_type == "path_step" and i.needs_doctor_review]
        self.assertTrue(required_steps, "样例里 S2 要求医生决定")
        summary = rs.summarize(items)
        self.assertIn(summary["pending"], range(1, summary["total"] + 1))
        self.assertIn("仍有", "".join(summary["blocking_reasons"]))
        self.assertFalse(summary["can_publish"])
        for item in items:
            if item.needs_doctor_review:
                item.state = rs.CONFIRMED
                item.decision_id = f"d-{item.item_id}"
        self.assertTrue(rs.summarize(items)["can_publish"])

    def test_doctor_can_still_override_exempt_item(self):
        """免审条目医生仍可接管：接受后就变成"医生已处理"，不再算免审（红线：人可覆盖规则）。"""
        items = rs.apply_decisions(rs.build_items(sample_case()), [])
        target = next(i for i in items if i.state == rs.AUTO_PROCESSED)
        decision = {"target_type": target.target_type, "target_id": target.target_id,
                    "action": "accept", "reason": "医生主动接管",
                    "reviewer": {"role": "医学生"}, "created_at": "9",
                    "decision_id": "d-1"}
        rs.apply_decisions(items, [decision])
        self.assertEqual(target.state, rs.CONFIRMED)
        summary = rs.summarize(items)
        self.assertEqual(target.item_id in [i.item_id for i in items
                                            if i.state == rs.AUTO_PROCESSED], False)
        self.assertEqual(summary["doctor_reviewed"], 1)
        self.assertEqual(summary["auto_processed"], 0)


class TestReviewFlowEndToEnd(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"))
        created = self.flow.create_case(medical_history=CASE_B, main_issue="该看哪个科", age=58)
        self.case_id = created.case_id
        self.assertTrue(created.ok, created.errors)

    def tearDown(self):
        self.flow.store.close()

    def _item_ids(self):
        review = self.flow.get_review(self.case_id)
        return [i["item_id"] for i in review["items"]], review

    def test_review_items_generated(self):
        ids, review = self._item_ids()
        self.assertTrue(ids, "应产生可审核条目")
        # 待审只算"必审"条目：未要求医生决定的路径步骤是免审（auto_processed）
        exempt = [i for i in review["items"] if not i["needs_doctor_review"]]
        self.assertTrue(exempt, "本例应有免审的路径步骤")
        self.assertEqual(review["summary"]["pending"], len(ids) - len(exempt))
        self.assertEqual(review["summary"]["auto_processed"], len(exempt))
        self.assertTrue(review["items"][0]["item_id"].startswith("department_selection:"))

    def test_accept_decision_recorded(self):
        ids, review = self._item_ids()
        first_required = next(i["item_id"] for i in review["items"] if i["needs_doctor_review"])
        before = review["summary"]["pending"]
        result = self.flow.submit_decision(self.case_id, first_required, "accept",
                                           reviewer_name="医学生")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["summary"]["pending"], before - 1)
        self.assertEqual(result["summary"]["doctor_reviewed"], 1)
        events = [a["event"] for a in self.flow.store.list_audit(self.case_id)]
        self.assertIn("decision_recorded", events)

    def test_modify_without_reason_rejected(self):
        ids, _ = self._item_ids()
        result = self.flow.submit_decision(self.case_id, ids[0], "modify", after="改一下")
        self.assertFalse(result["ok"])
        self.assertIn("原因", result["error"])

    def test_unknown_item_rejected(self):
        result = self.flow.submit_decision(self.case_id, "department_selection:不存在", "accept")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])

    def test_publish_blocked_while_pending(self):
        result = self.flow.publish_review(self.case_id, reviewer_name="医学生")
        self.assertFalse(result["ok"])
        self.assertIn("不能发布", result["error"])

    def test_publish_with_accept_all(self):
        ids, _ = self._item_ids()
        # 先人工修改一条，制造"人工痕迹"
        modified = self.flow.submit_decision(
            self.case_id, ids[1], "modify", after="改为：建议向医生确认是否需要眼部评估",
            reason="改写为面向患者的中性表述", reviewer_name="医学生")
        self.assertTrue(modified["ok"], modified.get("error"))
        result = self.flow.publish_review(self.case_id, reviewer_name="医学生", accept_all=True)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["case_state"], "doctor_confirmed")
        self.assertEqual(result["report_version"], 2)
        self.assertTrue(result["batch_accepted"], "批量接受应逐条留痕")
        case = self.flow.get_case(self.case_id)
        self.assertEqual(case["case_state"], "doctor_confirmed")
        self.assertEqual(int(case["report_version"]), 2)
        self.assertTrue((case["payload"]["review"] or {}).get("published"))
        events = [a["event"] for a in self.flow.store.list_audit(self.case_id)]
        self.assertIn("report_published", events)
        transitions = [(t["from_state"], t["to_state"])
                       for t in self.flow.store.list_transitions(self.case_id)]
        self.assertIn(("awaiting_doctor", "doctor_reviewed"), transitions)
        self.assertIn(("doctor_reviewed", "doctor_confirmed"), transitions)
        self.assertNotIn(("awaiting_doctor", "doctor_confirmed"), transitions)

    def test_request_more_info_sets_case_state(self):
        ids, _ = self._item_ids()
        result = self.flow.submit_decision(self.case_id, ids[0], "request_more_info",
                                           reason="缺少眼底检查结果", reviewer_name="医学生")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["state_changed"], "need_more_info")
        case = self.flow.get_case(self.case_id)
        self.assertEqual(case["case_state"], "need_more_info")

    def test_report_badge_turns_green_after_publish(self):
        self.flow.publish_review(self.case_id, reviewer_name="医学生", accept_all=True)
        reports = self.flow.build_reports(self.case_id)
        self.assertTrue(reports["summary"]["doctor_confirmed"])
        self.assertTrue(reports["full"]["doctor_confirmed"])

    def test_doctor_opinion_recorded_from_doctor_side(self):
        result = self.flow.add_update(self.case_id, kind="医生意见",
                                      content="建议眼科评估后行眼底激光",
                                      source="医生上传", department="眼科")
        self.assertTrue(result["ok"], result.get("error"))
        review = self.flow.get_review(self.case_id)
        self.assertTrue(review["doctor_opinions"])
        self.assertTrue(review["doctor_opinions"][0]["is_verified"])

    def test_list_cases_pending_priority(self):
        result = self.flow.list_review_cases()
        rows = result["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["case_id"], self.case_id)
        self.assertGreater(row["pending_items"], 0)
        self.assertEqual(row["behavior_type"], "B")
        self.assertIn(row["case_state"], ("awaiting_doctor", "analyzing"))

    def test_list_cases_filter(self):
        self.assertEqual(self.flow.list_review_cases(state_filter="doctor_confirmed")["rows"], [])
        self.assertEqual(len(self.flow.list_review_cases(state_filter="awaiting_doctor")["rows"]), 1)

    def test_history_has_timeline_and_decisions(self):
        ids, _ = self._item_ids()
        self.flow.submit_decision(self.case_id, ids[0], "uncertain", reason="证据不足",
                                  reviewer_name="医学生")
        history = self.flow.get_review_history(self.case_id)
        self.assertTrue(history["timeline"])
        self.assertTrue(history["decisions"])
        self.assertEqual(history["decisions"][0]["action_label"], "暂不确定")
        self.assertEqual(history["decisions"][0]["before"] is not None, True)

    def test_history_missing_case(self):
        self.assertIsNone(self.flow.get_review_history("不存在"))

    def test_uncertain_enters_pending_questions(self):
        ids, _ = self._item_ids()
        self.flow.submit_decision(self.case_id, ids[0], "uncertain", reason="待线下确认",
                                  reviewer_name="医学生")
        review = self.flow.get_review(self.case_id)
        self.assertTrue(review["pending_questions"])

    def test_no_probability_in_review_payload(self):
        text = json.dumps(self.flow.get_review(self.case_id), ensure_ascii=False)
        for token in ("概率", "置信度", "confidence"):
            self.assertNotIn(token, text)


class TestReviewQueuePagination(unittest.TestCase):
    def setUp(self):
        self.flow = CaseFlow(store=Store(":memory:"))
        for i in range(3):
            self.flow.store.save_case(
                f"Q{i}",
                case_state="awaiting_doctor",
                main_issue=f"队列病例 {i}",
                payload={"profile": {"chief_complaint": f"主诉 {i}"}},
            )

    def tearDown(self):
        self.flow.store.close()

    def test_review_queue_returns_page_metadata(self):
        result = self.flow.list_review_cases(limit=2, offset=1)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(result["rows"]), 2)
        self.assertFalse(result["has_more"])

    def test_review_queue_applies_state_filter_before_paging(self):
        self.flow.store.update_case("Q1", case_state="doctor_confirmed")
        result = self.flow.list_review_cases(
            limit=1, offset=0, state_filter="awaiting_doctor"
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["case_state"], "awaiting_doctor")

    def test_review_queue_returns_filter_counts_for_current_scope(self):
        result = self.flow.list_review_cases(limit=20)
        self.assertIsInstance(result["filters"], dict)
        self.assertEqual(result["filters"]["all"], 3)
        self.assertEqual(result["filters"]["doctor_pending"], 0)
        self.assertEqual(result["filters"]["exempt"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
