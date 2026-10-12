import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.env_loader import load_project_environment


class ProjectEnvironmentTests(unittest.TestCase):
    def test_loads_submission_materials_env_when_source_env_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "CHAIS"
            env_path = (
                source_root.parent
                / "competition-materials"
                / "local-config"
                / ".env"
            )
            env_path.parent.mkdir(parents=True)
            env_path.write_text(
                "APP_PATIENT_PASSWORD=materials-secret\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("APP_PATIENT_PASSWORD", None)
                load_project_environment(source_root)
                self.assertEqual(os.environ["APP_PATIENT_PASSWORD"], "materials-secret")


if __name__ == "__main__":
    unittest.main()
