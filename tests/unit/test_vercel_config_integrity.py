import copy
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("meal_vercel_config_integrity", ROOT / "deploy/vercel/verify_bundle.py")
verification = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verification)


class VercelConfigurationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.configuration = {
            "$schema": "https://openapi.vercel.sh/vercel.json", "framework": "fastapi",
            "buildCommand": "python verify_bundle.py",
            "functions": {"app.py": {"maxDuration": 300, "excludeFiles": "public/**"}},
            "headers": [{"source": "/assets/(.*)", "headers": [
                {"key": "X-Content-Type-Options", "value": "nosniff"},
                {"key": "X-Fictional-Label", "value": "café"},
            ]}],
        }
        self.raw_configuration = (json.dumps(self.configuration, indent=2) + "\n").encode()
        self.files = {"app.py": b"app = None\n", "requirements.txt": b"fastapi>=0.115,<1\n", "vercel.json": self.raw_configuration}
        for name in verification.ASSETS["admin"] | {"index.html"}:
            self.files["frontend/dist/admin/" + name] = ("fictional " + name).encode()
            if name != "index.html":
                self.files["public/assets/" + name] = ("fictional " + name).encode()
        for name, content in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        canonical = json.dumps(self.configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.manifest = {
            "format_version": 1, "application": "admin",
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in self.files.items()},
            "json_files": {"vercel.json": hashlib.sha256(canonical).hexdigest()},
        }
        self.write_manifest()

    def write_manifest(self):
        (self.root / "bundle-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def assert_configuration_failure(self, content):
        (self.root / "vercel.json").write_bytes(content)
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_CONFIGURATION_MISMATCH") as failed:
            verification.verify_bundle(self.root, {})
        diagnostic = verification.verification_diagnostic(failed.exception)
        self.assertEqual(diagnostic["code"], "VERCEL_BUNDLE_CONFIGURATION_MISMATCH")
        self.assertEqual(diagnostic["stage"], "FILE")
        self.assertEqual(diagnostic["file_entry"], 3)
        self.assertNotIn("fictional-secret", json.dumps(diagnostic))

    def test_canonical_hash_is_compact_sorted_utf8_and_preserves_raw_manifest_hash(self):
        self.assertEqual(verification.canonical_json_sha256(self.raw_configuration), self.manifest["json_files"]["vercel.json"])
        self.assertEqual(self.manifest["files"]["vercel.json"], hashlib.sha256(self.raw_configuration).hexdigest())
        self.assertEqual(verification.verify_bundle(self.root, {}), "admin")

    def test_whitespace_and_reordered_json_objects_are_accepted(self):
        reordered = dict(reversed(list(self.configuration.items())))
        reordered["functions"]["app.py"] = dict(reversed(list(reordered["functions"]["app.py"].items())))
        raw = json.dumps(reordered, indent=4, ensure_ascii=False).encode("utf-8")
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(), self.manifest["files"]["vercel.json"])
        (self.root / "vercel.json").write_bytes(raw)
        self.assertEqual(verification.verify_bundle(self.root, {}), "admin")

    def test_actual_javascript_json_stringify_representation_is_accepted_offline(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is required for the offline JSON.stringify regression")
        result = subprocess.run(
            [node, "-e", "globalThis.fetch=()=>{throw new Error('Network forbidden')};const fs=require('node:fs');process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0,'utf8'))));"],
            input=self.raw_configuration, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(result.stdout, self.raw_configuration)
        (self.root / "vercel.json").write_bytes(result.stdout)
        self.assertEqual(verification.verify_bundle(self.root, {}), "admin")

    def test_changed_added_and_removed_configuration_settings_are_rejected(self):
        changed = copy.deepcopy(self.configuration)
        changed["functions"]["app.py"]["maxDuration"] = 301
        added = dict(self.configuration, unknownSetting="fictional-secret")
        removed = dict(self.configuration)
        del removed["headers"]
        for candidate in (changed, added, removed):
            with self.subTest(candidate=list(candidate)):
                self.assert_configuration_failure(json.dumps(candidate).encode())

    def test_changed_build_command_security_header_and_exclusion_are_rejected(self):
        candidates = []
        for key, value in (("buildCommand", "echo fictional-secret"), ("framework", "other")):
            candidate = copy.deepcopy(self.configuration)
            candidate[key] = value
            candidates.append(candidate)
        candidate = copy.deepcopy(self.configuration)
        candidate["headers"][0]["headers"][0]["value"] = "fictional-secret"
        candidates.append(candidate)
        candidate = copy.deepcopy(self.configuration)
        candidate["functions"]["app.py"]["excludeFiles"] = "**"
        candidates.append(candidate)
        for candidate in candidates:
            self.assert_configuration_failure(json.dumps(candidate).encode())

    def test_duplicate_nonfinite_and_malformed_json_are_rejected(self):
        valid = json.dumps(self.configuration)
        duplicates = [valid[:-1] + ',"framework":"fastapi"}', valid.replace('"maxDuration": 300', '"maxDuration": 300,"maxDuration": 300')]
        candidates = duplicates + [valid.replace('"maxDuration": 300', '"maxDuration": ' + value) for value in ("NaN", "Infinity", "-Infinity", "1e999")]
        for candidate in candidates + ["fictional-secret", "[", '"fictional-secret"']:
            self.assert_configuration_failure(candidate.encode())

    def test_source_and_dependency_files_still_require_identical_bytes(self):
        for name in ("app.py", "requirements.txt"):
            path = self.root / name
            path.write_bytes(self.files[name] + b" ")
            with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_CHECKSUM_MISMATCH"):
                verification.verify_bundle(self.root, {})
            path.write_bytes(self.files[name])

    def test_legacy_manifest_keeps_vercel_json_byte_verification(self):
        del self.manifest["json_files"]
        self.write_manifest()
        self.assertEqual(verification.verify_bundle(self.root, {}), "admin")
        (self.root / "vercel.json").write_bytes(json.dumps(self.configuration).encode())
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_CHECKSUM_MISMATCH"):
            verification.verify_bundle(self.root, {})

    def test_malformed_json_files_maps_are_rejected(self):
        digest = self.manifest["json_files"]["vercel.json"]
        for value in (None, [], {}, {"app.py": digest}, {"vercel.json": digest, "app.py": digest}, {"vercel.json": "x" * 64}, {"vercel.json": True}):
            self.manifest["json_files"] = value
            self.write_manifest()
            with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_MANIFEST_INVALID"):
                verification.verify_bundle(self.root, {})

    def test_json_digest_requires_vercel_json_raw_entry_and_valid_raw_digest(self):
        digest = self.manifest["files"].pop("vercel.json")
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_MANIFEST_INVALID"):
            verification.verify_bundle(self.root, {})
        self.manifest["files"]["vercel.json"] = "invalid"
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_MANIFEST_INVALID"):
            verification.verify_bundle(self.root, {})
        self.manifest["files"]["vercel.json"] = digest

    def test_duplicate_manifest_keys_are_rejected(self):
        text = json.dumps(self.manifest)
        (self.root / "bundle-manifest.json").write_text(text[:-1] + ',"json_files":{}}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_MANIFEST_READ_FAILED"):
            verification.verify_bundle(self.root, {})

    def test_configuration_still_rejects_symlinks_and_multiple_hardlinks(self):
        path = self.root / "vercel.json"
        linked = self.root / "copy.json"
        os.link(path, linked)
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_LINK_COUNT_UNSUPPORTED"):
            verification.verify_bundle(self.root, {})
        linked.unlink()
        linked.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(linked)
        with self.assertRaisesRegex(RuntimeError, "VERCEL_BUNDLE_UNSAFE_PATH_SYMLINK"):
            verification.verify_bundle(self.root, {})


if __name__ == "__main__":
    unittest.main()
