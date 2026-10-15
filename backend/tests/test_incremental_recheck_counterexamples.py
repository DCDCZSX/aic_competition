"""增量复核反例：新资料无法归类、意见插入/删除、旧任务晚返回时，
都不能把旧审核决定沿用到新内容上。"""
import threading
import time
import unittest
from unittest import mock

from services import review_service, update_service
from services.case_flow import CaseFlow
from services.store import Store
from tests.test_mdt_screening import CASE_TEXT

BASE_TEXT = "2型糖尿病5年，视物模糊3个月，双下肢浮肿1周。检查尿蛋白++，血糖9.2。"


def analyses(departments, tag=""):
    return [{"department": d, "status": "ok", "focus": f"{d}关注点", "facts": [f"{d}事实"],
             "inferences": [{"text": f"{d}判断：需排查相关问题{tag}", "evidence_refs": ["S-1:0"]}],
             "checks": [{"content": f"{d}待确认"}], "information_gaps": []}
            for d in departments]


def mdt(departments, consensus, tag=""):
    return {"analyses": analyses(departments, tag),
            "aggregation": {"consensus": [{"topic": t, "supporters": list(departments[:2])}
                                          for t in consensus], "disagreements": []}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)
        result = self.flow.create_case(medical_history=BASE_TEXT, owner_id="p")
        self.assertTrue(result.ok, result.errors)
        self.cid = result.case_id
        self.flow._patch_payload(self.cid, mdt=mdt(["内分泌科", "肾内科", "眼科"], ["影像检查", "过敏史"]))
        self.accept_all()

    def tearDown(self):
        self.store.close()

    def accept_all(self):
        for item in self.flow.get_review(self.cid)["items"]:
            if item["state"] == "awaiting":
                res = self.flow.submit_decision(
                    self.cid, item["item_id"], "accept", reviewer_name="医生",
                    actor_username="lead", actor_role="doctor",
                    expected_content_revision=item["content_revision"], require_content_revision=True)
                self.assertTrue(res["ok"], res)

    def valid_ids(self):
        case = self.store.load_case(self.cid)
        items = review_service.build_items(case)
        return {f"{d['target_type']}:{d['target_id']}"
                for d in review_service.current_decisions(items, self.store.list_decisions(self.cid))}

    def specialty_ids(self):
        return {"ai_analysis:内分泌科", "ai_analysis:肾内科", "ai_analysis:眼科"}


class UnclassifiedMaterialTest(_Base):
    def test_unmapped_doctor_opinion_reopens_every_participating_specialty(self):
        self.assertTrue(self.specialty_ids() <= self.valid_ids())
        result = self.flow.add_update(self.cid, kind="医生意见", content="外院医生说可能需要排除淋巴瘤")
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.specialty_ids() & self.valid_ids(), "旧专科审核不能被静默沿用")
        self.assertIn(update_service.UNCLASSIFIED_NOTE, result["change_summary"]["notes"])

    def test_unmapped_clinical_kinds_are_conservative_but_schedule_is_not(self):
        for kind in update_service.CLINICAL_UPDATE_KINDS:
            affected = update_service.determine_affected(kind, "说不清的新情况", ["皮肤科"])
            self.assertEqual(affected["departments"], ["皮肤科"], kind)
            self.assertTrue(affected["unclassified"], kind)
        schedule = update_service.determine_affected("复查安排", "下月复诊", ["皮肤科"])
        self.assertEqual(schedule["departments"], [])
        self.assertFalse(schedule["unclassified"])

    def test_mapped_material_is_not_marked_unclassified(self):
        affected = update_service.determine_affected("检查结果", "眼底检查：视网膜病变",
                                                     ["眼科", "内分泌科", "肾内科"])
        self.assertEqual(affected["departments"], ["眼科", "内分泌科"])
        self.assertFalse(affected["unclassified"])

    def test_schedule_update_keeps_specialty_decisions(self):
        result = self.flow.add_update(self.cid, kind="复查安排", content="下月复诊")
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.specialty_ids() <= self.valid_ids())


class InsertDeleteTest(_Base):
    def test_removed_then_readded_specialty_does_not_reuse_old_approval(self):
        with mock.patch.object(self.flow, "_refresh_affected_mdt",
                               return_value=(mdt(["内分泌科", "肾内科"], ["过敏史"]), ["内分泌科"])):
            first = self.flow.add_update(self.cid, kind="检查结果", content="复查尿蛋白+，肌酐 80")
        self.assertIn("ai_analysis:眼科", first["change_summary"]["removed_item_ids"])
        changed = mdt(["内分泌科", "肾内科", "眼科"], ["眼底出血需复查", "过敏史"], tag="（新结论）")
        with mock.patch.object(self.flow, "_refresh_affected_mdt", return_value=(changed, ["眼科"])):
            second = self.flow.add_update(self.cid, kind="复查安排", content="下周复查眼底")
        self.assertIn("ai_analysis:眼科", second["change_summary"]["added_item_ids"])
        self.assertNotIn("ai_analysis:眼科", self.valid_ids())
        item = next(i for i in self.flow.get_review(self.cid)["items"] if i["item_id"] == "ai_analysis:眼科")
        self.assertEqual(item["state"], "awaiting")

    def test_inserted_consensus_shifts_positions_without_carrying_old_decision(self):
        shifted = mdt(["内分泌科", "肾内科", "眼科"], ["新增：肾功能评估", "影像检查", "过敏史"])
        with mock.patch.object(self.flow, "_refresh_affected_mdt", return_value=(shifted, ["肾内科"])):
            self.flow.add_update(self.cid, kind="复查安排", content="补充肾功能复查安排")
        items = {i["item_id"]: i for i in self.flow.get_review(self.cid)["items"]}
        self.assertIn("新增：肾功能评估", items["consensus_item:C-01"]["title"])
        valid = self.valid_ids()
        for item_id in ("consensus_item:C-01", "consensus_item:C-02", "consensus_item:C-03"):
            self.assertNotIn(item_id, valid, f"{item_id} 内容已改变或新出现，不能沿用旧决定")


class LateReturnTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)
        self.cid = self.flow.create_case(medical_history=CASE_TEXT).case_id
        self.flow.use_llm = True
        self.flow.ai_service = mock.Mock(api_key="local-test-key")

    def tearDown(self):
        self.store.close()

    def wait(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("等待超时")

    def test_update_waits_for_running_analysis_and_is_not_overwritten(self):
        started, release = threading.Event(), threading.Event()
        real_profile = self.flow._profile_to_dict

        def slow_profile(profile):
            started.set(); release.wait(5)
            return real_profile(profile)

        with mock.patch.object(self.flow, "_model_available", return_value=True), \
             mock.patch.object(self.flow, "_profile_to_dict", side_effect=slow_profile), \
             mock.patch.object(self.flow, "_run_mdt", return_value={"analyses": [], "aggregation": {}}):
            self.assertTrue(self.flow.start_analysis_job(self.cid, phases=["profile"]))
            self.assertTrue(started.wait(5))
            outcome = {}
            worker = threading.Thread(target=lambda: outcome.update(
                self.flow.add_update(self.cid, kind="检查结果", content="眼底检查：视网膜病变")))
            worker.start()
            time.sleep(.2)
            self.assertFalse(outcome, "补充资料必须等旧分析任务释放病例锁后再写入")
            release.set()
            worker.join(5)
        self.assertTrue(outcome.get("ok"), outcome)
        payload = self.store.load_case(self.cid)["payload"]
        self.assertIn("眼底检查：视网膜病变", payload["masked_history"])
        self.assertEqual(payload["updates"][-1]["content"], "眼底检查：视网膜病变")

    def test_simulation_regeneration_invalidates_historical_item_ids(self):
        self.flow.submit_doctor_routing(self.cid, primary_department="内分泌科", secondary_departments=["眼科"],
                                        decision="recommend_mdt", purpose="明确共同处理的问题",
                                        reason="共同讨论", reviewer_id="doctor")
        # 历史上出现过、本轮未显示的科室编号
        self.flow._patch_payload(self.cid, review_item_revisions={"ai_analysis:肾内科": 3})
        with mock.patch.object(self.flow, "_run_mdt", return_value={"analyses": [], "aggregation": {}}):
            self.assertTrue(self.flow.start_doctor_mdt_simulation(self.cid, reviewer_id="doctor")["ok"])
            self.wait(lambda: self.flow.get_doctor_mdt_simulation(self.cid)["state"] != "running")
        revisions = self.store.load_case(self.cid)["payload"]["review_item_revisions"]
        self.assertGreater(revisions["ai_analysis:肾内科"], 3)


if __name__ == "__main__":
    unittest.main()
