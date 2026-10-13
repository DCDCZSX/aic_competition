"""Cross-flow regressions for F01-F08; every test asserts the corrected behavior."""
import copy
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import app as web
from services.auth import AuthService
from services.case_flow import CaseFlow
from services.case_context import effective_case_text
from services.store import Store
from services import task_service, update_service
from services.portrait_extractor import extract
from services.ai_service import AIService
from tests.test_create_progress import FakeAI


class AuditHttpRegressions(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store)
        self.auth = AuthService(accounts={
            "alice": {"password": "audit-password", "role": "patient", "label": "患者"},
            "lead": {"password": "audit-password", "role": "doctor", "label": "主责医生",
                     "department_code": "endocrinology"},
            "eye": {"password": "audit-password", "role": "doctor", "label": "协同医生",
                    "department_code": "ophthalmology"},
        }, store=self.store)
        self.patch = mock.patch.multiple(web, _shared_store=self.store, case_flow=self.flow, auth_service=self.auth)
        self.patch.start()
        self.network = mock.patch("requests.post", side_effect=AssertionError("External network disabled"))
        self.network.start()
        self.client = web.app.test_client()
        self.headers = {name: {"Authorization": "Bearer " + self.auth.login(username=name, password="audit-password").token}
                        for name in ("alice", "lead", "eye")}

    def tearDown(self):
        self.network.stop()
        self.patch.stop()
        self.store.close()

    def post(self, cid, endpoint, user="alice", **body):
        return self.client.post(f"/api/v2/case/{cid}/{endpoint}", headers=self.headers[user], json=body)

    def create(self, text="2型糖尿病5年，视物模糊3个月。"):
        result = self.flow.create_case(medical_history=text, owner_id="alice")
        self.assertTrue(result.ok, result.errors)
        return result.case_id

    def ready(self):
        cid = self.create("2型糖尿病5年，视物模糊3个月，双下肢浮肿1周。检查尿蛋白++，血糖9.2。")
        self.flow._patch_payload(cid, gaps=[], mdt={"analyses": [], "aggregation": {}})
        self.store.grant_case_access(cid, "lead", granted_by="alice", case_role="primary")
        self.route(cid)
        self.accept_required(cid)
        response = self.post(cid, "publish", "lead", final_advice="携带资料向接诊医生确认后续安排。")
        self.assertEqual(response.status_code, 200, response.get_json())
        return cid

    def route(self, cid):
        response = self.post(cid, "doctor-routing", "lead", primary_department="内分泌科",
                             decision="single_specialty", reason="核对已有资料")
        self.assertEqual(response.status_code, 200, response.get_json())

    def accept_required(self, cid):
        for item in self.flow.get_review(cid)["items"]:
            if item["review_tier"] == "required" and item["state"] == "awaiting":
                response = self.post(cid, "decisions", "lead", item_id=item["item_id"], action="accept",
                                     expected_content_revision=item["content_revision"])
                self.assertEqual(response.status_code, 200, response.get_json())

    def test_f01_published_decisions_and_disagreements_are_locked(self):
        cid = self.ready()
        before = copy.deepcopy(self.store.load_case(cid))
        decisions = self.store.list_decisions(cid)
        for action in ("accept", "modify", "reject", "uncertain", "request_more_info"):
            response = self.post(cid, "decisions", "lead", item_id="path_step:S2", action=action,
                                 after="未经重新发布的内容", reason="重新考虑", expected_content_revision=1)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["code"], "review_already_published")
        response = self.post(cid, "disagreement-decisions", "lead", disagreement_id="X-01",
                             status="resolved", choice="改变处理", reason="重新考虑")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "review_already_published")
        self.assertEqual(self.store.list_decisions(cid), decisions)
        self.assertEqual(self.store.load_case(cid), before)

    def test_f01_published_report_comes_from_snapshot_even_if_tasks_change(self):
        cid = self.ready()
        before = self.flow.build_reports(cid)
        task = self.store.load_case(cid)["payload"]["path"]["tasks"][0]
        self.assertTrue(self.flow.update_task_state(cid, task["task_id"], "done")["ok"])
        self.assertEqual(self.flow.build_reports(cid), before)
        saved = self.store.get_report_version(cid, 2)["report_json"]
        self.assertEqual({key: before[key] for key in saved}, saved)

    def test_f01_new_materials_allow_new_round_without_changing_v2(self):
        cid = self.ready()
        snapshot = self.store.get_report_version(cid, 2)
        response = self.post(cid, "updates", kind="复查安排", content="下个月复诊时携带现有报告。")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.load_case(cid)["case_state"], "awaiting_recheck")
        self.route(cid)
        self.accept_required(cid)
        response = self.post(cid, "publish", "lead", final_advice="复诊时核对新增资料。")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["report_version"], 3)
        self.assertEqual(self.store.get_report_version(cid, 2), snapshot)
        self.assertEqual(self.flow.build_reports(cid)["doctor_result"]["report_version"], 3)

    def test_f02_decision_and_publish_use_session_identity(self):
        cid = self.create()
        self.store.grant_case_access(cid, "lead", granted_by="alice", case_role="primary")
        self.route(cid)
        item = self.flow.get_review(cid)["items"][0]
        response = self.post(cid, "decisions", "lead", item_id=item["item_id"], action="accept",
                             reviewer="冒名审核员", reviewer_dept="眼科", expected_content_revision=item["content_revision"])
        self.assertEqual(response.status_code, 200)
        record = self.store.list_decisions(cid)[-1]
        self.assertEqual(record["reviewer_name"], "主责医生")
        self.assertEqual(record["reviewer"]["dept"], "内分泌科")
        self.assertEqual(record["actor_username"], "lead")
        self.assertEqual(record["actor_role"], "doctor")
        self.assertEqual(record["actor_department_code"], "endocrinology")
        self.accept_required(cid)
        response = self.post(cid, "publish", "lead", reviewer="冒名审核员", final_advice="携带资料复诊。")
        self.assertEqual(response.status_code, 200, response.get_json())
        review = self.store.load_case(cid)["payload"]["review"]
        self.assertEqual(review["published_by"], "主责医生")
        self.assertEqual(review["actor_username"], "lead")
        self.assertEqual(self.store.get_report_version(cid, 2)["review_summary"]["actor_username"], "lead")

    def test_f02_disagreement_uses_session_identity(self):
        cid = self.create()
        self.store.grant_case_access(cid, "lead", granted_by="alice", case_role="primary")
        payload = self.store.load_case(cid)["payload"]
        payload["mdt"] = {"analyses": [], "aggregation": {"disagreements": [{"topic": "待核对资料",
            "positions": {"眼科": "需要核对", "内分泌科": "补充记录"}}]}}
        self.flow._patch_payload(cid, mdt=payload["mdt"])
        response = self.post(cid, "disagreement-decisions", "lead", disagreement_id="X-01", status="resolved",
                             choice="核对后处理", reason="按现有资料", reviewer="冒名审核员", reviewer_dept="眼科")
        self.assertEqual(response.status_code, 200, response.get_json())
        record = self.store.load_case(cid)["payload"]["disagreement_decisions"][-1]
        self.assertEqual(record["actor_username"], "lead")
        self.assertEqual(record["reviewer_name"], "主责医生")

    def test_f03_updates_keep_answer_evidence_in_triage_and_history_clean(self):
        cid = self.create("高血压10年，近期血压控制平稳。")
        answer = self.post(cid, "answers", answers={"urine_protein": "尿蛋白++"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn("肾内科", answer.get_json()["triage"]["selected"])
        for index in range(2):
            response = self.post(cid, "updates", kind="复查安排", content=f"第{index + 1}次补充复诊安排。")
            self.assertEqual(response.status_code, 200)
            self.assertIn("肾内科", response.get_json()["triage"]["selected"])
        payload = self.store.load_case(cid)["payload"]
        self.assertEqual(effective_case_text(payload).count("尿蛋白++"), 1)
        self.assertNotIn("尿蛋白++", payload["masked_history"])

    def test_f03_background_triage_also_uses_saved_answers(self):
        cid = self.create("高血压10年，近期血压控制平稳。")
        self.post(cid, "answers", answers={"urine_protein": "尿蛋白++"})
        self.flow.ai_service = FakeAI()
        self.flow.use_llm = True
        self.flow._analysis_worker(cid, ["triage"])
        self.assertIn("肾内科", self.store.load_case(cid)["payload"]["triage"]["selected"])

    def test_f03_deferred_mdt_receives_the_same_effective_evidence(self):
        cid = self.create("高血压10年，近期血压控制平稳。")
        self.flow.ai_service = FakeAI()
        self.flow.use_llm = True
        original = self.flow._run_mdt
        texts = []
        def capture(**kwargs):
            texts.append(kwargs["text"])
            return original(**kwargs)
        with mock.patch.object(self.flow, "_run_mdt", side_effect=capture):
            response = self.post(cid, "answers", answers={"urine_protein": "++"}, defer_mdt=True)
            self.assertEqual(response.status_code, 200)
            deadline = time.monotonic() + 5
            while self.flow._active_jobs and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.flow._active_jobs)
        self.assertTrue(texts)
        self.assertTrue(all("尿蛋白 / 肾功能：++" in text for text in texts))

    def test_f06_legacy_rule_profile_survives_enhancement(self):
        cid = self.create()
        self.flow._patch_payload(cid, demographics=None, profile_rules={"age": 58, "sex": "女"})
        payload = self.store.load_case(cid)["payload"]
        payload.pop("demographics")
        self.store.save_case(cid, owner_id="alice", case_state="awaiting_doctor", payload=payload)
        self.flow.ai_service = SimpleNamespace(api_key="offline-key", call_ai_api=lambda *a, **kw: None)
        self.flow._analysis_worker(cid, ["profile"])
        profile = self.store.load_case(cid)["payload"]["profile"]
        self.assertEqual((profile["age"], profile["sex"]), (58, "女"))

    def test_f04_empty_answers_are_idempotent_and_preserve_done_history(self):
        cid = self.create()
        task = self.store.load_case(cid)["payload"]["path"]["tasks"][0]
        self.assertTrue(self.flow.update_task_state(cid, task["task_id"], "done")["ok"])
        before = self.store.load_case(cid)
        response = self.post(cid, "answers", answers={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["idempotent"])
        self.assertEqual(self.store.load_case(cid), before)

    def test_f04_repeated_identical_answers_do_not_start_another_round(self):
        cid = self.create()
        self.assertEqual(self.post(cid, "answers", answers={"urine_protein": "尿蛋白++"}).status_code, 200)
        before = self.store.load_case(cid)
        response = self.post(cid, "answers", answers={"urine_protein": "尿蛋白++"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["idempotent"])
        self.assertEqual(self.store.load_case(cid), before)

    def test_f05_negative_answer_does_not_remove_exam_gap(self):
        cid = self.create()
        response = self.post(cid, "answers", answers={"fundus_exam": "尚未做眼底检查，结果暂缺。"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("眼底检查" in gap for gap in response.get_json()["remaining_gaps"]))
        self.assertIn("fundus_exam", response.get_json()["unresolved_fields"])

    def test_f04_answers_updates_and_worker_preserve_unchanged_tasks(self):
        cid = self.create()
        original = self.store.load_case(cid)["payload"]["path"]["tasks"][0]
        self.assertTrue(self.flow.update_task_state(cid, original["task_id"], "cancelled")["ok"])
        expected = self.store.load_case(cid)["payload"]["path"]["tasks"][0]
        self.assertEqual(self.post(cid, "answers", answers={"urine_protein": "尿蛋白++"}).status_code, 200)
        self.assertEqual(self.post(cid, "updates", kind="复查安排", content="携带现有报告复诊。").status_code, 200)
        self.flow.ai_service = FakeAI()
        self.flow._analysis_worker(cid, ["triage"])
        tasks = self.store.load_case(cid)["payload"]["path"]["tasks"]
        current = next(task for task in tasks if task["source"] == expected["source"] and task["content"] == expected["content"])
        for key in ("task_id", "state", "history"):
            self.assertEqual(current[key], expected[key])
        self.assertEqual(len({task["task_id"] for task in tasks}), len(tasks))

    def test_f05_pending_exam_keeps_gap_and_result_records_evidence(self):
        cid = self.create()
        for content in ("尚未做眼底检查，检查结果暂缺。", "眼底检查已做，结果未出。", "预约眼底检查。"):
            response = self.post(cid, "updates", kind="检查结果", content=content)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(any("眼底检查" in gap for gap in self.store.load_case(cid)["payload"]["gaps"]))
        response = self.post(cid, "updates", kind="检查结果", content="眼底检查未见异常。")
        self.assertEqual(response.status_code, 200)
        payload = self.store.load_case(cid)["payload"]
        self.assertFalse(any("眼底检查" in gap for gap in payload["gaps"]))
        self.assertTrue(any("眼底检查" in item["gap"] for item in payload["gap_resolution_evidence"]))

    def test_f06_normal_progressive_pipeline_preserves_demographics(self):
        for provider in (FakeAI(), SimpleNamespace(api_key="offline-key", call_ai_api=lambda *a, **kw: None)):
            with self.subTest(provider=type(provider).__name__):
                self.flow.use_llm = True
                self.flow.ai_service = provider
                result = self.flow.create_case(medical_history="2型糖尿病5年，视物模糊3个月。",
                                               age=58, sex="女", owner_id="alice", progressive=True)
                deadline = time.monotonic() + 5
                while self.flow._active_jobs and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(self.flow._active_jobs)
                profile = self.store.load_case(result.case_id)["payload"]["profile"]
                self.assertEqual((profile["age"], profile["sex"]), (58, "女"))
                self.flow._analysis_worker(result.case_id, ["profile"])
                profile = self.store.load_case(result.case_id)["payload"]["profile"]
                self.assertEqual((profile["age"], profile["sex"]), (58, "女"))

    def test_f06_conflicting_text_keeps_confirmed_fields_and_flags_conflict(self):
        result = self.flow.create_case(medical_history="男，60岁，2型糖尿病5年。", age=58, sex="女")
        self.assertTrue(result.ok, result.errors)
        self.assertEqual((result.profile["age"], result.profile["sex"]), (58, "女"))
        self.assertEqual({item["field"] for item in result.profile["demographic_conflicts"]}, {"age", "sex"})

    def test_f07_sharing_does_not_promote_and_formal_submission_can_assign(self):
        cid = self.create()
        self.store.grant_case_access(cid, "eye", granted_by="lead", case_role="collaborator")
        response = self.client.delete(f"/api/v2/case/{cid}/access", headers=self.headers["alice"], json={"reviewer_id": "eye"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post(cid, "access", reviewer_id="eye").status_code, 200)
        self.assertEqual(self.store.get_case_access_role(cid, "eye"), "collaborator")
        response = self.post(cid, "doctor-routing", "eye", primary_department="眼科", decision="single_specialty", reason="核对资料")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post(cid, "submit-review", department_code="ophthalmology").status_code, 200)
        self.assertEqual(self.store.get_case_access_role(cid, "eye"), "primary")
        self.assertTrue(any(row["event"] == "case_role_changed" for row in self.store.list_audit(cid)))

    def test_f08_aliases_rejected_before_token_consumption_and_no_pii_saved(self):
        preview = self.client.post("/api/v2/preview", headers=self.headers["alice"],
                                   json={"medical_history": "2型糖尿病5年，视物模糊3个月。"}).get_json()
        token = preview["preview_token"]
        for field in ("sex", "gender"):
            for value in ("audit.person@example.com", "13812345678", "M", "a" * 100):
                response = self.client.post("/api/v2/case", headers=self.headers["alice"],
                                            json={"preview_token": token, field: value})
                self.assertEqual(response.status_code, 400, response.get_json())
        response = self.client.post("/api/v2/case", headers=self.headers["alice"],
                                    json={"preview_token": token, "gender": "男", "sex": "女"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v2/case", headers=self.headers["alice"], json={"preview_token": token, "sex": "女"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.store.load_case(response.get_json()["case_id"])["payload"]["profile"]["sex"], "女")


class EvidenceAndTaskRegressions(unittest.TestCase):
    def test_exam_availability_distinguishes_pending_and_provided_results(self):
        for text in ("未做眼底检查", "眼底检查待做", "拟行眼底检查", "眼底检查已做，报告未出", "仅提及眼底检查",
                     "没有眼底检查报告", "眼底检查尚未完成"):
            with self.subTest(text=text):
                self.assertNotIn("眼底检查", update_service.gaps_filled_by(text))
                self.assertTrue(any("眼底检查" in gap for gap in extract(text, case_id="EVIDENCE").gaps))
        for text in ("眼底检查未见异常", "眼底检查：视网膜病变", "眼底检查已提供报告"):
            self.assertIn("眼底检查", update_service.gaps_filled_by(text))

    def test_changed_completed_task_reopens_with_history_and_unique_ids(self):
        old = [{"task_id": "T-001", "type": "就医咨询", "source": "眼科:checks", "content": "旧安排",
                "state": "done", "history": [{"from": "todo", "to": "done"}]}]
        new = [{"task_id": "T-001", "type": "资料准备", "source": "new", "content": "新增资料", "state": "todo"},
               {"task_id": "T-002", "type": "就医咨询", "source": "眼科:checks", "content": "新安排", "state": "todo"}]
        merged = task_service.merge_progress(old, new, reason="资料变化")
        changed = merged[1]
        self.assertEqual(changed["task_id"], "T-001")
        self.assertEqual(changed["state"], "reopened")
        self.assertEqual(changed["history"][-1]["from"], "done")
        self.assertEqual(changed["history"][-1]["to"], "reopened")
        self.assertEqual(len(changed["history"]), 2)
        self.assertNotEqual(merged[0]["task_id"], changed["task_id"])
        self.assertEqual(old[0]["state"], "done")

    def test_model_boundary_blocks_pii_without_external_request(self):
        with mock.patch.dict(os.environ, {"AI_API_KEY": "offline-test-key"}):
            service = AIService()
        with mock.patch("requests.post") as network:
            result = service.call_ai_api_raw("患者性别：audit.person@example.com")
        network.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ai_sensitive_input")

    def test_model_boundary_checks_system_prompt_and_masks_nonblocking_identity(self):
        with mock.patch.dict(os.environ, {"AI_API_KEY": "offline-test-key"}):
            service = AIService()
        response = mock.Mock()
        response.json.return_value = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        with mock.patch("requests.post", return_value=response) as network:
            blocked = service.call_ai_api_raw("整理资料", role_description="电话13812345678")
            self.assertEqual(blocked["error_code"], "ai_sensitive_input")
            network.assert_not_called()
            service.call_ai_api_raw("姓名：张三，男，58岁，2型糖尿病5年。")
            messages = network.call_args.kwargs["json"]["messages"]
            self.assertNotIn("张三", json.dumps(messages, ensure_ascii=False))
