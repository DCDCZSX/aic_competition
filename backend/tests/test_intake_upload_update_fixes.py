"""回归：主诉不取人口学开头、图片上传不受 JSON 全局上限限制、补充资料入口可用。"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.portrait_extractor import (  # noqa: E402
    _extract_chief_complaint,
    _usable_model_chief,
)


class ChiefComplaintDemographicsTest(unittest.TestCase):
    def assert_chief(self, text, expected):
        chief, refs = _extract_chief_complaint(text)
        self.assertEqual(chief, expected)
        if expected:
            self.assertIn(chief, text, "主诉必须是原文直引")
            self.assertEqual(refs, [f"S-1:{text.find(chief)}"])

    def test_leading_sex_and_age_sentence_is_skipped(self):
        self.assert_chief("女，58岁。反复乏力、面色苍白3个月，加重1周。",
                          "反复乏力、面色苍白3个月，加重1周")

    def test_leading_demographic_clauses_are_stripped(self):
        self.assert_chief("患者女，58岁，反复乏力3个月，近1周牙龈出血。既往高血压。",
                          "反复乏力3个月，近1周牙龈出血")
        self.assert_chief("58岁男性，体检发现血糖升高2年。", "体检发现血糖升高2年")
        self.assert_chief("男 62岁 反复咳嗽", "反复咳嗽")
        self.assert_chief("性别：女；年龄：58岁；咳嗽咳痰2周", "咳嗽咳痰2周")

    def test_explicit_chief_field_still_wins(self):
        self.assert_chief("主诉：反复胸闷1年", "反复胸闷1年")
        self.assert_chief("主诉：女，58岁，头晕2周", "头晕2周")

    def test_demographics_only_text_leaves_gap(self):
        self.assert_chief("女。58岁。", "")

    def test_model_chief_must_be_quote_and_not_demographic(self):
        raw = "女，58岁。反复乏力、面色苍白3个月"
        self.assertEqual(_usable_model_chief("反复乏力、面色苍白3个月", raw),
                         "反复乏力、面色苍白3个月")
        self.assertEqual(_usable_model_chief("女，58岁", raw), "")
        self.assertEqual(_usable_model_chief("贫血待查", raw), "")


class UploadLimitAndPatientUpdateEntryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ.setdefault("APP_DB_PATH", os.path.join(cls.tmp.name, "t.db"))
        os.environ.setdefault("APP_PATIENT_UPLOAD_DIR", os.path.join(cls.tmp.name, "uploads"))
        import app as app_module
        cls.app_module = app_module
        cls.app = app_module.app

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_upload_route_allows_single_image_above_global_json_limit(self):
        from flask import request
        previous = self.app.config["MAX_CONTENT_LENGTH"]
        self.app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
        try:
            body = b"\x89PNG\r\n\x1a\n" + os.urandom(3 * 1024 * 1024)
            with self.app.test_request_context(
                    "/api/v2/patient/uploads", method="POST",
                    data={"files": (io.BytesIO(body), "scan.png", "image/png")},
                    content_type="multipart/form-data"):
                request.max_content_length = self.app_module.UPLOAD_REQUEST_MAX_BYTES
                self.assertEqual(len(request.files.getlist("files")), 1)
            self.assertGreaterEqual(self.app_module.UPLOAD_REQUEST_MAX_BYTES,
                                    self.app_module.MAX_UPLOAD_BYTES)
        finally:
            self.app.config["MAX_CONTENT_LENGTH"] = previous

    def test_oversized_request_returns_chinese_json_error(self):
        from werkzeug.exceptions import RequestEntityTooLarge
        with self.app.test_request_context("/api/v2/preview", method="POST"):
            response, status = self.app_module._api_http_error(RequestEntityTooLarge())
        self.assertEqual(status, 413)
        self.assertEqual(response.get_json()["code"], "request_entity_too_large")
        self.assertIn("提交内容过大", response.get_json()["error"])

    def test_patient_page_has_update_entry_and_ai_source_labels(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "templates", "patient.html"), encoding="utf-8") as fh:
            patient = fh.read()
        self.assertIn('id="patient-update-panel"', patient)
        self.assertIn("/updates`", patient)
        self.assertIn("openPatientCaseForUpdate(", patient)
        self.assertIn("专科 AI 参考意见 · 非医生结论", patient)
        with open(os.path.join(root, "templates", "doctor.html"), encoding="utf-8") as fh:
            doctor = fh.read()
        self.assertIn("AI 模拟 · 独立研判", doctor)
        self.assertIn("来源：本次 AI MDT 模拟", doctor)


if __name__ == "__main__":
    unittest.main()
