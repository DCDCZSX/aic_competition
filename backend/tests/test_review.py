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


class TestRiskGroupsAndBatchPlan(unittest.TestCase):
    """详情收敛（7.3）：风险分级 / 三组折叠 / 批量影响面预览。"""

    def setUp(self):
        self.items = rs.build_items(sample_case())
        self.by_id = {i.item_id: i for i in self.items}

    def test_disagreement_is_high_risk(self):
        disputes = [i for i in self.items
                    if i.target_type == "consensus_item" and i.target_id.startswith("X-")]
        self.assertTrue(disputes, "样例病例应有分歧条目")
        for item in disputes:
            self.assertEqual(item.risk_level, "高", item.item_id)

    def test_exempt_step_is_low_risk(self):
        # S1 未要求医生决定 → 免审（不进批量清单：它本来就不需要医生操作）
        self.assertFalse(self.by_id["path_step:S1"].needs_doctor_review)
        # S2 要求医生决定 → 低风险必审 → 可以进批量清单
        plan = rs.batch_plan(self.items)
        ids = {row["item_id"] for row in plan["accept"]}
        self.assertIn("path_step:S2", ids)
        self.assertNotIn("path_step:S1", ids)
        self.assertTrue(rs.is_batch_low_risk(self.by_id["path_step:S2"]))
        # 免审条目在 apply_decisions 之后是"低风险"，且不属于必审
        rs.apply_decisions(self.items, [])
        self.assertEqual(self.by_id["path_step:S1"].risk_level, "低")
        self.assertEqual(self.by_id["path_step:S1"].state, rs.AUTO_PROCESSED)

    def test_dispute_never_batchable(self):
        """红线：分歧待裁决不能走批量通道。"""
        for item in self.items:
            if item.target_type == "consensus_item":
                self.assertFalse(rs.is_batch_low_risk(item), item.item_id)

    def test_plan_explains_exclusions(self):
        plan = rs.batch_plan(self.items)
        self.assertTrue(plan["excluded"], "应列出必须逐条判断的条目")
        for row in plan["excluded"]:
            self.assertTrue(row["why"])
        accepted_ids = set(plan["accept_ids"])
        for row in plan["excluded"]:
            self.assertNotIn(row["item_id"], accepted_ids)
        # 预览与执行共用同一份判定：accept + excluded = 全部待审必审条目
        pending = [i.item_id for i in self.items if i.needs_doctor_review and i.state == rs.AWAITING]
        self.assertEqual(sorted(list(accepted_ids) + [r["item_id"] for r in plan["excluded"]]),
                         sorted(pending))

    def test_groups_partition_all_items(self):
        groups = rs.group_items(self.items)
        merged = groups["must"] + groups["decided"] + groups["exempt"]
        self.assertEqual(sorted(merged), sorted(i.item_id for i in self.items))
        self.assertEqual(len(merged), len(set(merged)), "条目不能同时出现在两组")

    def test_decided_item_moves_group(self):
        item = self.by_id["department_selection:内分泌科"]
        self.assertIn(item.item_id, rs.group_items(self.items)["must"])
        decision = {"target_type": "department_selection", "target_id": "内分泌科",
                    "action": "accept", "reviewer": {"role": "医学生"}, "created_at": "2026-09-17T10:00:00",
                    "decision_id": "d1"}
        rs.apply_decisions(self.items, [decision])
        groups = rs.group_items(self.items)
        self.assertIn(item.item_id, groups["decided"])
        self.assertNotIn(item.item_id, groups["must"])

    def test_reopen_raises_exempt_item_to_required(self):
        """撤销 = 医生要自己重新处理：免审条目也要变成必审（否则会出现"待审但不计入待审数"）。"""
        item = self.by_id["path_step:S1"]
        self.assertFalse(item.needs_doctor_review)
        decision = {"target_type": "path_step", "target_id": "S1", "action": "reopen",
                    "reason": "我要自己看", "reviewer": {"role": "医学生"},
                    "created_at": "2026-09-17T10:00:00", "decision_id": "d1"}
        rs.apply_decisions([item], [decision])
        self.assertEqual(item.state, rs.AWAITING)
        self.assertTrue(item.needs_doctor_review)
        self.assertEqual(item.risk_level, "中")


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
        """待审 = **必审且未处理**；免审条目（未要求医生决定的路径步骤）不计入待审。"""
        items = rs.build_items(sample_case())
        applied = rs.apply_decisions(items, [])          # 走一遍派生（auto_processed）
        s0 = rs.summarize(applied)
        exempt = [i for i in applied if not i.needs_doctor_review]
        self.assertTrue(exempt, "样例里应有免审的路径步骤")
        self.assertEqual(s0["pending"], s0["total"] - len(exempt))
        self.assertEqual(s0["auto_processed"], len(exempt))
        self.assertEqual(s0["doctor_reviewed"], 0)
        self.assertFalse(s0["can_publish"])
        for item in applied:
            item.state = rs.CONFIRMED
        s1 = rs.summarize(applied)
        self.assertTrue(s1["can_publish"])
        self.assertEqual(s1["progress"], 1.0)

    def test_exempt_items_do_not_block_publishing(self):
        """真缺陷回归：免审条目曾是 awaiting，仅此就把发布卡死。

        现在只要"必审条目"都终态即可发布，且在报告/审计里**不能**算成医生确认。
        """
        items = rs.apply_decisions(rs.build_items(sample_case()), [])
        for item in items:
            if not item.needs_doctor_review:
                self.assertEqual(item.state, rs.AUTO_PROCESSED, item.item_id)
                self.assertTrue(item.auto_reason, "免审要有可解释的原因")
            else:
                item.state = rs.CONFIRMED
                item.decision_id = "D-1"
                item.reviewer = "doctor"
        summary = rs.summarize(items)
        self.assertEqual(summary["pending"], 0)
        self.assertTrue(summary["can_publish"], summary)
        self.assertGreater(summary["auto_processed"], 0)
        # 免审不能计入"医生处理过"
        self.assertEqual(summary["doctor_reviewed"], summary["total"] - summary["auto_processed"])

    def test_required_step_still_blocks_publishing(self):
        """反向保护：要求医生决定的路径步骤仍然必须审完才能发布。"""
        case = sample_case()
        for step in (case["payload"]["path"]["steps"]):
            step["requires_doctor"] = True
        items = rs.apply_decisions(rs.build_items(case), [])
        summary = rs.summarize(items)
        self.assertEqual(summary["pending"], summary["total"])
        self.assertFalse(summary["can_publish"])
        self.assertTrue(summary["blocking_reasons"], summary)
        self.assertIn("必审条目", "；".join(summary["blocking_reasons"]))

    def test_doctor_can_still_override_exempt_item(self):
        """免审不等于不可干预：医生若对免审条目做了决定，以医生决定为准（不标成自动处理）。"""
        case = sample_case()
        items = rs.build_items(case)
        exempt = next(i for i in items if not i.needs_doctor_review)
        decisions = [{"target_type": exempt.target_type, "target_id": exempt.target_id,
                      "action": "reject", "reason": "该步骤不适用本例",
                      "decision_id": "D-9", "created_at": "1",
                      "reviewer": {"role": "doctor"}}]
        applied = {i.item_id: i for i in rs.apply_decisions(items, decisions)}
        self.assertEqual(applied[exempt.item_id].state, rs.REJECTED)
        self.assertEqual(applied[exempt.item_id].decision_id, "D-9")
        self.assertEqual(applied[exempt.item_id].auto_reason, "")

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
        rows = self.flow.list_review_cases()["rows"]
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

    # ---------------- 详情收敛 7.3：批量接受（预览 → 执行 → 撤销）----------------
    def _inject_mdt_disagreement(self):
        """给病例注入一条多学科分歧（真实链路要跑 LLM，单测里直接写 payload）。"""
        def mutate(case):
            payload = case.setdefault("payload", {})
            payload["mdt"] = {"aggregation": {
                "consensus": [],
                "disagreements": [{"topic": "先查眼底 vs 先查肾功",
                                   "positions": {"眼科": "先查眼底", "肾内科": "先查肾功"},
                                   "resolving_info": "需补充检查依据"}],
                "open_questions": [],
            }}
        self.flow.store.mutate_case(self.case_id, mutate)

    def test_preview_batch_is_read_only(self):
        review = self.flow.get_review(self.case_id)
        before = review["summary"]["pending"]
        plan = self.flow.preview_batch(self.case_id)
        self.assertTrue(plan["ok"], plan.get("error"))
        self.assertTrue(plan["accept"], "应给出将接受的条目")
        self.assertEqual(self.flow.get_review(self.case_id)["summary"]["pending"], before,
                         "预览不得改动任何条目")
        self.assertTrue(plan["note"])

    def test_batch_accept_records_batch_id(self):
        plan = self.flow.preview_batch(self.case_id)
        result = self.flow.submit_batch(self.case_id, reviewer_name="医学生")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["accepted_count"], len(plan["accept_ids"]))
        self.assertTrue(result["batch_id"].startswith("B-"))
        self.assertEqual(result["active_batch"], result["batch_id"])
        decisions = self.flow.store.list_decisions(self.case_id)
        tagged = [d for d in decisions if d.get("batch_id") == result["batch_id"]]
        self.assertEqual(len(tagged), len(plan["accept_ids"]))
        events = [a["event"] for a in self.flow.store.list_audit(self.case_id)]
        self.assertIn("batch_accepted", events)
        # 批量接受留的是一条批次审计 + 每条独立决策（不是静默通过，也不是 N 条噪音）
        self.assertEqual(events.count("decision_recorded"), 0)

    def test_batch_can_be_undone(self):
        before = self.flow.get_review(self.case_id)["summary"]["pending"]
        reviewed_before = self.flow.get_review(self.case_id)["summary"]["doctor_reviewed"]
        batch = self.flow.submit_batch(self.case_id, reviewer_name="医学生")
        self.assertTrue(batch["ok"], batch.get("error"))
        mid = self.flow.get_review(self.case_id)["summary"]["pending"]
        self.assertEqual(mid, before - batch["accepted_count"])
        undo = self.flow.undo_batch(self.case_id, batch["batch_id"], reviewer_name="医学生",
                                    reason="误点，需要逐条看")
        self.assertTrue(undo["ok"], undo.get("error"))
        self.assertEqual(undo["undone_count"], batch["accepted_count"])
        after = self.flow.get_review(self.case_id)
        self.assertEqual(after["summary"]["pending"], before, "撤销后条目应回到待审")
        # 撤销后不能既算"待审"又算"医生已处理"（计数必须自洽）
        self.assertEqual(after["summary"]["doctor_reviewed"], reviewed_before,
                         "撤销后医生处理数应回退")
        self.assertEqual(after["active_batch"], "", "批次已撤销，不应再显示可撤销")
        events = [a["event"] for a in self.flow.store.list_audit(self.case_id)]
        self.assertIn("batch_undone", events)
        # 历史只增不删：接受记录与撤销记录都在
        decisions = self.flow.store.list_decisions(self.case_id)
        actions = [d.get("action") for d in decisions if d.get("batch_id") == batch["batch_id"]]
        self.assertEqual(actions.count("accept"), batch["accepted_count"])
        self.assertEqual(actions.count("reopen"), batch["accepted_count"])

    def test_undo_twice_is_rejected(self):
        batch = self.flow.submit_batch(self.case_id, reviewer_name="医学生")
        self.assertTrue(self.flow.undo_batch(self.case_id, batch["batch_id"],
                                             reason="第一次撤销")["ok"])
        again = self.flow.undo_batch(self.case_id, batch["batch_id"], reason="再撤一次")
        self.assertFalse(again["ok"])
        self.assertIn("已被撤销", again["error"])

    def test_undo_reason_required_and_unknowable_batch(self):
        batch = self.flow.submit_batch(self.case_id, reviewer_name="医学生")
        no_reason = self.flow.undo_batch(self.case_id, batch["batch_id"], reason="")
        # 撤销必须写明原因：空原因时整批不会被动（条目仍是已接受）
        self.assertFalse(no_reason["ok"])
        self.assertTrue(no_reason.get("need_reason"))
        self.assertEqual(self.flow.get_review(self.case_id)["active_batch"], batch["batch_id"])
        self.assertFalse(self.flow.undo_batch(self.case_id, "B-不存在", reason="x")["ok"])

    def test_reopen_requires_reason_and_pending_item(self):
        review = self.flow.get_review(self.case_id)
        required = next(i["item_id"] for i in review["items"] if i["needs_doctor_review"])
        self.flow.submit_decision(self.case_id, required, "accept", reviewer_name="医学生")
        no_reason = self.flow.submit_decision(self.case_id, required, "reopen")
        self.assertFalse(no_reason["ok"])
        self.assertIn("原因", no_reason["error"])
        ok = self.flow.submit_decision(self.case_id, required, "reopen", reason="要重新看",
                                       reviewer_name="医学生")
        self.assertTrue(ok["ok"], ok.get("error"))
        self.assertEqual(ok["state_changed"], None)
        # 已经回到待审的条目再撤一次没有意义
        again = self.flow.submit_decision(self.case_id, required, "reopen", reason="再撤")
        self.assertFalse(again["ok"])

    def test_same_second_undo_beats_accept(self):
        """created_at 只到秒：同一秒内的"接受→撤销"必须按写入顺序判定，否则撤销会静默失效。"""
        review = self.flow.get_review(self.case_id)
        required = next(i["item_id"] for i in review["items"] if i["needs_doctor_review"])
        self.flow.submit_decision(self.case_id, required, "accept", reviewer_name="医学生")
        self.flow.submit_decision(self.case_id, required, "reopen", reason="同一秒撤销",
                                  reviewer_name="医学生")
        item = next(i for i in self.flow.get_review(self.case_id)["items"]
                    if i["item_id"] == required)
        self.assertEqual(item["state"], "awaiting")

    def test_publish_accept_all_leaves_disputes_for_doctor(self):
        """批量通道不能替医生裁决分歧：即使 accept_all=true，分歧条目仍会阻塞发布并如实说明。"""
        self._inject_mdt_disagreement()
        result = self.flow.publish_review(self.case_id, reviewer_name="医学生", accept_all=True)
        self.assertFalse(result["ok"], result)
        self.assertIn("不能发布", result["error"])
        self.assertTrue(any(i.startswith("consensus_item:X-") for i in result["pending_items"]),
                        result["pending_items"])
        review = self.flow.get_review(self.case_id)
        dispute = next(i for i in review["items"] if i["item_id"].startswith("consensus_item:X-"))
        self.assertEqual(dispute["state"], "awaiting")
        self.assertEqual(dispute["risk_level"], "高")
        # 逐条接受分歧后即可发布（医生做了判断）
        self.flow.submit_decision(self.case_id, dispute["item_id"], "accept",
                                  reviewer_name="医学生")
        done = self.flow.publish_review(self.case_id, reviewer_name="医学生")
        self.assertTrue(done["ok"], done.get("error"))

    def test_undo_refused_after_publish(self):
        self.flow.publish_review(self.case_id, reviewer_name="医学生", accept_all=True)
        batch_id = (self.flow.get_case(self.case_id)["payload"]["review"] or {}).get("batch_id")
        self.assertTrue(batch_id, "发布时应记录批次号")
        result = self.flow.undo_batch(self.case_id, batch_id, reason="发布后反悔")
        self.assertFalse(result["ok"])
        self.assertIn("已发布", result["error"])

    def test_sample_exempt_is_read_only_and_audited(self):
        """抽检免审条目：只读（不改状态、不算医生处理），但要在审计里留下"抽了哪几条"。"""
        review = self.flow.get_review(self.case_id)
        exempt = [i["item_id"] for i in review["items"] if i["state"] == "auto_processed"]
        self.assertTrue(exempt, "本例应有免审条目")
        before = review["summary"]
        result = self.flow.sample_exempt(self.case_id, k=2, reviewer_name="医学生")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["sample"]), min(2, len(exempt)))
        self.assertTrue(all(s["item_id"] in exempt for s in result["sample"]))
        self.assertEqual(result["exempt_total"], len(exempt))
        after = self.flow.get_review(self.case_id)["summary"]
        self.assertEqual(after["pending"], before["pending"], "抽检不得改变待审数")
        self.assertEqual(after["doctor_reviewed"], before["doctor_reviewed"],
                         "抽检不得记成医生处理（红线：不能给免审条目偷偷盖医生章）")
        self.assertEqual(after["auto_processed"], before["auto_processed"])
        for item in self.flow.get_review(self.case_id)["items"]:
            if item["item_id"] in [s["item_id"] for s in result["sample"]]:
                self.assertEqual(item["state"], "auto_processed", "抽检后条目仍是免审态")
        events = [a["event"] for a in self.flow.store.list_audit(self.case_id)]
        self.assertIn("exempt_sampled", events)

    def test_sample_size_is_clamped_to_available(self):
        result = self.flow.sample_exempt(self.case_id, k=999)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["sample"]), result["exempt_total"])

    def test_sample_without_exempt_items_is_rejected(self):
        review = self.flow.get_review(self.case_id)
        # 把所有免审条目都接管（撤销 → 升为必审），此时没有可抽检对象
        for item in review["items"]:
            if item["state"] == "auto_processed":
                self.flow.submit_decision(self.case_id, item["item_id"], "accept",
                                          reviewer_name="医学生")
        result = self.flow.sample_exempt(self.case_id)
        self.assertFalse(result["ok"])
        self.assertIn("没有免审条目", result["error"])

    def test_sampled_item_can_be_taken_over_by_doctor(self):
        """抽检发现问题的正确处置：医生直接接管 → 才计入医生处理。"""
        result = self.flow.sample_exempt(self.case_id, k=1)
        item_id = result["sample"][0]["item_id"]
        taken = self.flow.submit_decision(self.case_id, item_id, "accept",
                                          reason="抽检确认可接受", reviewer_name="医学生")
        self.assertTrue(taken["ok"], taken.get("error"))
        item = next(i for i in self.flow.get_review(self.case_id)["items"]
                    if i["item_id"] == item_id)
        self.assertEqual(item["state"], "confirmed")
        self.assertTrue(item["decision_id"])
        self.assertGreaterEqual(self.flow.get_review(self.case_id)["summary"]["doctor_reviewed"], 1)

    def test_review_payload_exposes_groups(self):
        review = self.flow.get_review(self.case_id)
        self.assertEqual(set(review["groups"]), {"must", "decided", "exempt"})
        self.assertTrue(review["groups"]["exempt"], "本例应有免审条目")
        self.assertTrue(review["group_hints"]["exempt"])
        every = review["groups"]["must"] + review["groups"]["decided"] + review["groups"]["exempt"]
        self.assertEqual(sorted(every), sorted(i["item_id"] for i in review["items"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
