"""本轮后端加固回归：隐私、输入契约、旧病例状态与批处理扫描。"""

from __future__ import annotations

import os
import time
import unittest
from unittest import mock

import app as app_module
from services.auth import AuthService, ROLE_DOCTOR, ROLE_PATIENT
from services.case_flow import CaseFlow
from services.store import Store


ACCOUNTS = {
    "alice": {"password": "alice-password", "role": ROLE_PATIENT, "label": "患者 A"},
    "doctor": {"password": "doctor-password", "role": ROLE_DOCTOR, "label": "审核医生"},
}


class CaseFlowHardeningTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.flow = CaseFlow(store=self.store, use_llm=False)

    def tearDown(self):
        self.store.close()

    def test_update_blocks_blocking_pii_and_masks_non_blocking_pii(self):
        created = self.flow.create_case(
            medical_history="2型糖尿病5年，视物模糊3个月，尿蛋白++。",
            main_issue="该看哪个科",
        )
        self.assertTrue(created.ok, created.errors)

        blocked = self.flow.add_update(
            created.case_id,
            kind="检查结果",
            content="复查结果已出，联系电话13812345678，邮箱 test@example.com",
        )
        self.assertFalse(blocked["ok"])
        self.assertIn("敏感信息", blocked["error"])
        self.assertNotIn("13812345678", blocked["error"])
        self.assertNotIn("test@example.com", blocked["error"])

        masked = self.flow.add_update(
            created.case_id,
            kind="症状变化",
            content="患者：张三，现住北京市朝阳区某路 1 号，症状较前减轻",
        )
        self.assertTrue(masked["ok"], masked)
        saved = self.flow.get_case(created.case_id)["payload"]
        text = str(saved["updates"][-1]["content"])
        self.assertNotIn("张三", text)
        self.assertNotIn("北京市朝阳区某路", text)
        self.assertIn("张*", text)

    def test_update_rejects_overlong_content_without_mutating_case(self):
        created = self.flow.create_case(
            medical_history="2型糖尿病5年，视物模糊3个月，尿蛋白++。",
            main_issue="该看哪个科",
        )
        before = self.flow.get_case(created.case_id)["payload"]
        result = self.flow.add_update(
            created.case_id, kind="检查结果", content="眼底检查：" + "正常" * 8001
        )
        self.assertFalse(result["ok"])
        after = self.flow.get_case(created.case_id)["payload"]
        self.assertEqual(before.get("updates"), after.get("updates"))

    def test_initial_full_masked_history_is_persisted(self):
        tail = "；末尾关键记录：肌酐96μmol/L，需核对肾功能趋势。"
        history = ("2型糖尿病5年，视物模糊3个月，尿蛋白++。" + "既往检查记录。" * 500 + tail)
        self.assertLessEqual(len(history), 8000)
        created = self.flow.create_case(medical_history=history, main_issue="该看哪个科")
        self.assertTrue(created.ok, created.errors)
        saved = self.flow.get_case(created.case_id)["payload"]["masked_history"]
        self.assertIn("末尾关键记录", saved)
        self.assertGreater(len(saved), 2000)

    def test_legacy_failed_mdt_is_visible_and_old_case_is_scanned(self):
        self.store.save_case(
            "OLD-FAILED", case_state="awaiting_doctor",
            payload={
                "masked_history": "2型糖尿病5年，尿蛋白++，视物模糊。",
                "profile": {"chief_complaint": "视物模糊"},
                "triage": {"selected": ["内分泌科", "肾内科"]},
                "mdt": {"analyses": [
                    {"department": "内分泌科", "status": "ok"},
                    {"department": "肾内科", "status": "unavailable"},
                ]},
            },
        )
        queue = self.flow.list_review_cases()
        row = next(item for item in queue["rows"] if item["case_id"] == "OLD-FAILED")
        self.assertEqual(row["analysis_state"], "partial_failed")
        self.assertEqual(row["risk_level"], "高")
        with mock.patch.object(self.flow, "start_analysis_job", return_value=True):
            self.assertIn("OLD-FAILED", self.flow.analyze_pending_cases(limit=1)["started"])


class HttpInputHardeningTest(unittest.TestCase):
    def setUp(self):
        self.old_store = app_module._shared_store
        self.old_flow = app_module.case_flow
        self.old_auth = app_module.auth_service
        self.store = Store(":memory:")
        app_module._shared_store = self.store
        app_module.case_flow = CaseFlow(store=self.store, use_llm=False)
        app_module.auth_service = AuthService(accounts=ACCOUNTS)
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module._shared_store = self.old_store
        app_module.case_flow = self.old_flow
        app_module.auth_service = self.old_auth
        self.store.close()

    def _headers(self):
        response = self.client.post("/api/login", json={
            "username": "alice", "password": "alice-password",
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return {"Authorization": "Bearer " + response.get_json()["session"]["token"]}

    def test_json_shape_errors_are_json_400(self):
        headers = self._headers()
        response = self.client.post(
            "/api/v2/preview", headers=headers,
            json={"medical_history": {"bad": "shape"}, "main_issue": "问题"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.get_json().get("code"), "invalid_request")

        response = self.client.post(
            "/api/v2/preview", headers=headers, data="[1, 2]",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get("code"), "invalid_json")

    def test_limit_and_phases_errors_are_json_400(self):
        # 医生端批处理参数不能让 ValueError 泄漏成 HTML 500。
        doctor_login = self.client.post("/api/login", json={
            "username": "doctor", "password": "doctor-password",
        })
        doctor_headers = {"Authorization": "Bearer " + doctor_login.get_json()["session"]["token"]}
        response = self.client.post(
            "/api/v2/cases/analyze", headers=doctor_headers, json={"limit": "abc"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get("code"), "invalid_request")

        response = self.client.post(
            "/api/v2/cases/analyze", headers=doctor_headers, json={"limit": 99}
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.get(
            "/api/v2/cases?limit=abc", headers=doctor_headers
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json().get("code"), "invalid_request")

    def test_login_failure_is_generic_and_rate_limited(self):
        with mock.patch.dict(os.environ, {
            "APP_LOGIN_MAX_FAILURES": "2",
            "APP_LOGIN_COOLDOWN_SECONDS": "60",
        }):
            auth = AuthService(accounts=ACCOUNTS)
            for _ in range(2):
                with self.assertRaises(Exception) as ctx:
                    auth.login(username="not-a-real-user", password="wrong", client_key="ip")
                self.assertIn("账号或密码", str(ctx.exception))
            with self.assertRaises(Exception) as ctx:
                auth.login(username="alice", password="alice-password", client_key="ip")
            self.assertIn("账号或密码", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
