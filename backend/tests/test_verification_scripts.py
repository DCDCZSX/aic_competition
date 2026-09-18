from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class VerificationScriptContractTest(unittest.TestCase):
    def test_report_verifier_uses_env_credentials_and_preview_token(self):
        source = (ROOT / "verify_report_render.mjs").read_text(encoding="utf-8")

        self.assertIn("APP_PATIENT_PASSWORD", source)
        self.assertIn("/api/v2/preview", source)
        self.assertIn("preview_token", source)
        self.assertNotIn("password: 'patient'", source)
        self.assertNotIn('password: "patient"', source)

    def test_doctor_verifier_explicitly_grants_case_access(self):
        source = (ROOT / "verify_doctor_http.py").read_text(encoding="utf-8")

        self.assertIn("/api/v2/case/{case_id}/access", source)
        self.assertIn('"reviewer_id"', source)

    def test_user_facing_privacy_copy_describes_external_model_boundary(self):
        paths = (
            ROOT.parent / "README.md",
            ROOT / "templates" / "home.html",
            ROOT / "templates" / "patient.html",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("数据不出本机", source, str(path))
            self.assertNotIn("病例数据仅保存在本机", source, str(path))
            self.assertIn("外部模型", source, str(path))
