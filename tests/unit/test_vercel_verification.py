import contextlib
import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("meal_vercel_verification_diagnostics", ROOT / "deploy/vercel/verify_bundle.py")
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


class VercelVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.secret = "fictional-secret-must-never-appear"
        self.files = {"app.py": b"app = None\n", "backend/runtime.py": b"VALUE = 1\n"}
        for name in verification.ASSETS["admin"] | {"index.html"}:
            self.files["frontend/dist/admin/" + name] = ("fictional " + name).encode()
            if name != "index.html":
                self.files["public/assets/" + name] = ("fictional " + name).encode()
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.manifest = {
            "format_version": 1, "application": "admin",
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in self.files.items()},
        }
        (self.root / "bundle-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def failure(self, code, stage, *, environment=None):
        with self.assertRaisesRegex(RuntimeError, code) as failed:
            verification.verify_bundle(self.root, {} if environment is None else environment)
        diagnostic = verification.verification_diagnostic(failed.exception)
        self.assertEqual(diagnostic["code"], code)
        self.assertEqual(diagnostic["stage"], stage)
        self.assertNotIn(self.secret, json.dumps(diagnostic))
        self.assertNotIn(str(self.root), json.dumps(diagnostic))
        return diagnostic

    def test_complete_bundle_passes_without_diagnostics(self):
        self.assertEqual(verification.verify_bundle(self.root, {}), "admin")

    def test_missing_payload_reports_one_based_manifest_entry_and_safe_errno(self):
        (self.root / "backend/runtime.py").unlink()
        diagnostic = self.failure("VERCEL_BUNDLE_PAYLOAD_MISSING", "FILE")
        self.assertEqual(diagnostic["file_entry"], 2)
        self.assertEqual(diagnostic["error_type"], "FileNotFoundError")
        self.assertEqual(diagnostic["errno"], 2)

    def test_missing_and_invalid_manifest_report_manifest_stage(self):
        path = self.root / "bundle-manifest.json"
        path.unlink()
        diagnostic = self.failure("VERCEL_BUNDLE_MANIFEST_READ_FAILED", "MANIFEST")
        self.assertEqual(diagnostic["error_type"], "FileNotFoundError")
        self.assertNotIn("file_entry", diagnostic)
        path.write_text(self.secret, encoding="utf-8")
        diagnostic = self.failure("VERCEL_BUNDLE_MANIFEST_READ_FAILED", "MANIFEST")
        self.assertEqual(diagnostic["error_type"], "JSONDecodeError")

    def test_tampered_payload_reports_checksum_without_bytes_or_path(self):
        (self.root / "app.py").write_text(self.secret, encoding="utf-8")
        diagnostic = self.failure("VERCEL_BUNDLE_CHECKSUM_MISMATCH", "FILE")
        self.assertEqual(diagnostic["file_entry"], 1)

    def test_real_hardlink_still_fails_with_distinct_link_count_diagnostic(self):
        os.link(self.root / "app.py", self.root / "hardlink-copy.py")
        self.assertEqual((self.root / "app.py").stat().st_nlink, 2)
        diagnostic = self.failure("VERCEL_BUNDLE_LINK_COUNT_UNSUPPORTED", "FILE")
        self.assertEqual(diagnostic["file_entry"], 1)

    def test_symlink_still_fails_with_distinct_unsafe_path_diagnostic(self):
        path = self.root / "app.py"
        target = self.root / "fictional-source.py"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        diagnostic = self.failure("VERCEL_BUNDLE_UNSAFE_PATH_SYMLINK", "FILE")
        self.assertEqual(diagnostic["file_entry"], 1)

    def test_wrong_runtime_role_does_not_disclose_environment_value(self):
        diagnostic = self.failure("VERCEL_BUNDLE_APPLICATION_MISMATCH", "MANIFEST", environment={"MEAL_APPLICATION": self.secret})
        self.assertNotIn("file_entry", diagnostic)

    def test_static_and_frontend_mismatches_have_separate_stages(self):
        added = self.root / "public/assets/private.txt"
        added.write_text(self.secret, encoding="utf-8")
        diagnostic = self.failure("VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH", "STATIC")
        self.assertNotIn("file_entry", diagnostic)
        added.unlink()
        (self.root / "frontend/dist/scanner").mkdir()
        self.failure("VERCEL_BUNDLE_APPLICATION_MISMATCH", "FRONTEND")

    def test_unexpected_exception_is_sanitized_and_retains_only_safe_type(self):
        with patch.object(verification.hashlib, "file_digest", side_effect=ValueError(self.secret)):
            diagnostic = self.failure("VERCEL_BUNDLE_UNEXPECTED_ERROR", "FILE")
        self.assertEqual(diagnostic["file_entry"], 1)
        self.assertEqual(diagnostic["error_type"], "ValueError")

    def test_diagnostic_rejects_untrusted_codes_stages_types_indexes_and_errnos(self):
        error = verification.VerificationError(self.secret)
        for value in (self.secret, True, -1, 1000001):
            error.stage = self.secret
            error.error_type = self.secret
            error.file_entry = value
            error.errno = value
            diagnostic = verification.verification_diagnostic(error)
            self.assertEqual(diagnostic, {"code": "VERCEL_BUNDLE_UNEXPECTED_ERROR", "stage": "UNKNOWN", "error_type": "Exception"})

    def test_main_reports_only_controlled_diagnostic_without_traceback(self):
        output = io.StringIO()
        errors = io.StringIO()
        exception_type = type(self.secret, (Exception,), {})
        with patch.object(verification, "verify_bundle", side_effect=exception_type(self.secret)), patch.dict(os.environ, {"MEAL_APPLICATION": self.secret}), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = verification.main()
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn(self.secret, errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())
        self.assertIn('"code": "VERCEL_BUNDLE_UNEXPECTED_ERROR"', errors.getvalue())
        self.assertIn('"stage": "UNKNOWN"', errors.getvalue())


if __name__ == "__main__":
    unittest.main()
