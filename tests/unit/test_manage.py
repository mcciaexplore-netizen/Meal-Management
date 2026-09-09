import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ManagementLauncherTests(unittest.TestCase):
    def test_launcher_finds_backend_without_editable_install_or_pythonpath(self):
        root = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["MEAL_RUN_MYSQL_TESTS"] = "0"
        environment["MEAL_ALLOW_TEST_SCHEMA_CHANGES"] = "0"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-S", str(root / "manage.py"), "--help"],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check-config", result.stdout)
        self.assertIn("bootstrap-admin", result.stdout)


if __name__ == "__main__":
    unittest.main()
