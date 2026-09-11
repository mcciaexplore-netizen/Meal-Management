import importlib.util
import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


packaging = load_file("meal_vercel_packaging", ROOT / "deploy" / "vercel" / "package.py")
verification = load_file("meal_vercel_bundle_verification", ROOT / "deploy" / "vercel" / "verify_bundle.py")


class VercelPackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / "source"
        backend = self.project / "backend" / "meal_management"
        backend.mkdir(parents=True)
        for name in packaging.RUNTIME_MODULES:
            (backend / (name + ".py")).write_text("VALUE = " + repr(name) + "\n", encoding="utf-8")
        (self.project / "pyproject.toml").write_text(
            '[project]\nname = "fictional-meal-app"\ndependencies = ["fastapi>=0.115,<1"]\n'
            '[project.optional-dependencies]\ntest = ["httpx>=0.28,<1"]\n'
            'production = ["boto3>=1.35,<2"]\nvercel = ["vercel>=0.3,<1"]\n',
            encoding="utf-8",
        )
        self.private_files = (
            ".env", ".env.aiven", ".env.vercel", "var/private/photos/private.jpg", "var/private/backups/data.sql",
            "var/private/certificates/ca.pem", ".venv/lib/private.py", "frontend/node_modules/private.js",
            "backend/meal_management/private_secret.py", "backend/meal_management/backup.py",
            "backend/meal_management/transfer.py", "backend/meal_management/runtime_account.py",
            "var/private/vercel/database-runtime.env", "var/private/vercel/database-runtime.pending.env",
            "tests/private_fixture.json", "database/migrations/001_initial.sql",
        )
        for name in self.private_files:
            path = self.project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("FICTIONAL_PRIVATE_MARKER", encoding="utf-8")

    def _build(self, project_root, output):
        for role, assets in packaging.ASSETS.items():
            (output / role).mkdir(parents=True)
            for name in (*assets, "index.html"):
                (output / role / name).write_text(role + " " + name, encoding="utf-8")

    def _package(self, role="admin", name=None):
        destination = self.root / (name or role)
        with patch.object(packaging, "_build_frontend", side_effect=self._build):
            summary = packaging.package_application(role, project_root=self.project, output_directory=destination)
        return destination, summary

    def test_each_project_contains_only_its_frontend_assets(self):
        for role in packaging.ASSETS:
            with self.subTest(role=role):
                directory, summary = self._package(role)
                self.assertEqual(summary["application"], role)
                self.assertEqual({path.name for path in (directory / "frontend" / "dist").iterdir()}, {role})
                self.assertEqual({path.name for path in (directory / "public" / "assets").iterdir()}, set(packaging.ASSETS[role]))
                self.assertFalse((directory / "public" / "index.html").exists())
                self.assertTrue((directory / "frontend" / "dist" / role / "index.html").is_file())
                self.assertEqual(verification.verify_bundle(directory, {}), role)

    def test_explicit_allowlist_excludes_all_private_and_operational_inputs(self):
        directory, _ = self._package()
        files = [path for path in directory.rglob("*") if path.is_file()]
        names = {path.relative_to(directory).as_posix() for path in files}
        self.assertFalse(names.intersection(self.private_files))
        self.assertTrue(all(b"FICTIONAL_PRIVATE_MARKER" not in path.read_bytes() for path in files))
        self.assertFalse((directory / "database").exists())
        self.assertFalse((directory / "api").exists())
        self.assertEqual({path.name for path in directory.glob("*.py")}, {"app.py", "verify_bundle.py"})
        manifest = json.loads((directory / "bundle-manifest.json").read_text())
        self.assertEqual(names, set(manifest["files"]) | {"bundle-manifest.json", ".vercelignore"})

    def test_vercelignore_defaults_to_excluding_every_unlisted_upload(self):
        directory, _ = self._package("scanner")
        lines = (directory / ".vercelignore").read_text().splitlines()
        self.assertEqual(lines[0], "*")
        manifest = json.loads((directory / "bundle-manifest.json").read_text())
        expected = set(manifest["files"]) | {"bundle-manifest.json", ".vercelignore"}
        directories = {parent.as_posix() for name in expected for parent in Path(name).parents if parent != Path(".")}
        self.assertEqual({line[2:] for line in lines[1:]}, expected | directories)
        self.assertTrue(all(line.startswith("!/") and not line.endswith("/") for line in lines[1:]))
        self.assertTrue(all("*" not in line for line in lines[1:]))
        self.assertTrue(all("!/" + name not in lines for name in self.private_files))

    def test_installed_cli_upload_traversal_includes_complete_bundles_and_excludes_unlisted_files(self):
        engine = ROOT / "build/vercel-tools/node_modules/vercel/dist/chunks/chunk-562OXD3F.js"
        node = shutil.which("node")
        if not node or not engine.is_file():
            self.skipTest("Installed Vercel 59.15.1 and Node are required for this offline upload regression")
        script = """
globalThis.fetch = () => { throw new Error('Network access is forbidden'); };
const {require_dist} = await import(process.argv[1]);
const {buildFileTree} = require_dist();
const {relative} = await import('node:path');
const root = process.argv[2];
const result = await buildFileTree(root, {isDirectory:true, prebuilt:false}, () => {});
process.stdout.write(JSON.stringify(result.fileList.map(path => relative(root, path)).sort()));
"""

        def selected(directory):
            result = subprocess.run(
                [node, "--input-type=module", "-e", script, engine.as_uri(), str(directory)],
                cwd=self.root, env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "CI": "1", "VERCEL_TELEMETRY_DISABLED": "1"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0)
            return set(json.loads(result.stdout))

        for role in packaging.ASSETS:
            with self.subTest(role=role):
                directory, _ = self._package(role)
                manifest = json.loads((directory / "bundle-manifest.json").read_text())
                expected = set(manifest["files"]) | {"bundle-manifest.json", ".vercelignore"}
                directories = {parent.as_posix() for name in expected for parent in Path(name).parents if parent != Path(".")}
                extra = {
                    ".env", ".vercel/project.json", "backend/meal_management/private_secret.py",
                    "backend/meal_management/.env", "backend/meal_management/shadow/app.py",
                    "frontend/dist/" + role + "/private.env", "public/assets/private.txt",
                    "frontend/dist/" + ("scanner" if role == "admin" else "admin") + "/index.html",
                }
                for name in extra:
                    path = directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("FICTIONAL_PRIVATE_MARKER", encoding="utf-8")
                (directory / ".env.local").symlink_to(self.project / ".env")
                ignore = directory / ".vercelignore"
                corrected = ignore.read_bytes()
                old_lines = ["*"] + ["!" + name + "/" for name in sorted(directories)] + ["!" + name for name in sorted(expected)]
                ignore.write_text("\n".join(old_lines) + "\n", encoding="utf-8")
                old_selected = selected(directory)
                self.assertEqual(old_selected, {name for name in expected if "/" not in name})
                self.assertEqual(len(old_selected), 7)
                ignore.write_bytes(corrected)
                uploaded = selected(directory)
                self.assertEqual(uploaded, expected)
                self.assertFalse(uploaded.intersection(extra | {".env.local"}))
                target = self.root / (role + "-uploaded")
                target.mkdir()
                for name in uploaded:
                    path = target / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(directory / name, path)
                self.assertEqual(verification.verify_bundle(target, {}), role)

    def test_entry_uses_fixed_role_and_refuses_conflicting_environment_before_factory(self):
        directory, _ = self._package("scanner")
        runtime = types.ModuleType("meal_management.vercel_runtime")
        runtime.create_vercel_app = Mock(return_value=object())
        with patch.dict(sys.modules, {"meal_management.vercel_runtime": runtime}), patch.object(sys, "path", list(sys.path)):
            with patch.dict(os.environ, {"MEAL_APPLICATION": "scanner"}, clear=True):
                result = runpy.run_path(str(directory / "app.py"))
            self.assertIs(result["app"], runtime.create_vercel_app.return_value)
            runtime.create_vercel_app.assert_called_once_with("scanner")
            runtime.create_vercel_app.reset_mock()
            with patch.dict(os.environ, {"MEAL_APPLICATION": "admin"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "VERCEL_APPLICATION_MISMATCH"):
                    runpy.run_path(str(directory / "app.py"))
            runtime.create_vercel_app.assert_not_called()

    def test_requirements_include_only_runtime_and_vercel_extra(self):
        directory, _ = self._package()
        self.assertEqual((directory / "requirements.txt").read_text().splitlines(), ["fastapi>=0.115,<1", "vercel>=0.3,<1"])
        self.assertEqual((directory / ".python-version").read_text(), "3.12\n")

    def test_vercel_configuration_keeps_single_entry_and_cdn_security_headers(self):
        directory, _ = self._package()
        config = json.loads((directory / "vercel.json").read_text())
        self.assertEqual(config["framework"], "fastapi")
        self.assertEqual(config["buildCommand"], "python verify_bundle.py")
        self.assertEqual(config["regions"], ["bom1"])
        self.assertEqual(config["functions"], {"app.py": {"maxDuration": 300, "excludeFiles": "public/**"}})
        self.assertNotIn("builds", config)
        self.assertNotIn("rewrites", config)
        self.assertEqual(config["headers"][0]["source"], "/assets/(.*)")
        headers = {item["key"]: item["value"] for item in config["headers"][0]["headers"]}
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Security-Policy"], packaging.CONTENT_SECURITY_POLICY)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        raw = (directory / "vercel.json").read_bytes()
        manifest = json.loads((directory / "bundle-manifest.json").read_text())
        self.assertEqual(manifest["files"]["vercel.json"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest["json_files"], {"vercel.json": verification.canonical_json_sha256(raw)})

    def test_existing_output_is_preserved_and_never_overwritten(self):
        output = self.root / "existing"
        output.mkdir()
        (output / "keep.txt").write_text("keep")
        with patch.object(packaging, "_build_frontend") as build:
            with self.assertRaisesRegex(packaging.PackagingError, "OUTPUT_ALREADY_EXISTS"):
                packaging.package_application("admin", project_root=self.project, output_directory=output)
        build.assert_not_called()
        self.assertEqual((output / "keep.txt").read_text(), "keep")

    def test_symlinked_source_and_output_are_rejected(self):
        source = self.project / "backend" / "meal_management" / "security.py"
        source.unlink()
        source.symlink_to(self.project / ".env")
        with self.assertRaisesRegex(packaging.PackagingError, "REFUSES_SYMLINKS"):
            self._package()
        source.unlink()
        source.write_text("VALUE = 1\n")
        link = self.root / "linked"
        link.symlink_to(self.root / "missing-output")
        with self.assertRaisesRegex(packaging.PackagingError, "REFUSES_SYMLINKS"):
            packaging.package_application("admin", project_root=self.project, output_directory=link)

    def test_failed_build_creates_no_ready_package(self):
        output = self.root / "failure"
        with patch.object(packaging, "_build_frontend", side_effect=packaging.PackagingError("BUILD_FAILED")):
            with self.assertRaisesRegex(packaging.PackagingError, "BUILD_FAILED"):
                packaging.package_application("admin", project_root=self.project, output_directory=output)
        self.assertFalse(output.exists())
        self.assertFalse(list(self.root.glob("meal-vercel-build-*")))

    def test_runtime_import_outside_allowlist_fails_before_build(self):
        source = self.project / "backend" / "meal_management" / "config.py"
        source.write_text("from .private_secret import VALUE\n")
        with patch.object(packaging, "_build_frontend") as build:
            with self.assertRaisesRegex(packaging.PackagingError, "RUNTIME_IMPORT_NOT_ALLOWLISTED"):
                packaging.package_application("admin", project_root=self.project, output_directory=self.root / "unlisted")
        build.assert_not_called()

    def test_verifier_rejects_tampered_source_and_wrong_role(self):
        directory, _ = self._package()
        with self.assertRaisesRegex(RuntimeError, "APPLICATION_MISMATCH"):
            verification.verify_bundle(directory, {"MEAL_APPLICATION": "scanner"})
        (directory / "app.py").write_text("app = None\n")
        with self.assertRaisesRegex(RuntimeError, "CHECKSUM_MISMATCH"):
            verification.verify_bundle(directory, {})

    def test_verifier_rejects_extra_public_file_and_other_frontend(self):
        directory, _ = self._package()
        added = directory / "public" / "assets" / "scan-app.js"
        added.write_text("scanner")
        with self.assertRaisesRegex(RuntimeError, "STATIC_ASSETS_MISMATCH"):
            verification.verify_bundle(directory, {})
        added.unlink()
        (directory / "frontend" / "dist" / "scanner").mkdir()
        with self.assertRaisesRegex(RuntimeError, "APPLICATION_MISMATCH"):
            verification.verify_bundle(directory, {})

    def test_verifier_rejects_symlinked_file(self):
        directory, _ = self._package()
        path = directory / "public" / "assets" / "app.js"
        path.unlink()
        path.symlink_to(directory / "frontend" / "dist" / "admin" / "app.js")
        with self.assertRaisesRegex(RuntimeError, "UNSAFE_PATH"):
            verification.verify_bundle(directory, {})

    def test_packaging_does_not_change_source_frontend_outputs(self):
        original = self.project / "frontend" / "dist" / "existing-output.txt"
        original.parent.mkdir(parents=True)
        original.write_text("keep local build")
        self._package()
        self.assertEqual(original.read_text(), "keep local build")

    def test_installed_frontend_builder_produces_expected_assets_offline(self):
        if not (ROOT / "frontend" / "node_modules" / "esbuild").is_dir():
            self.skipTest("Installed frontend build dependencies are required")
        output = self.root / "actual-frontend"
        packaging._build_frontend(ROOT, output)
        for role, assets in packaging.ASSETS.items():
            self.assertEqual({path.name for path in (output / role).iterdir()}, set(assets) | {"index.html"})
            self.assertTrue(all((output / role / name).stat().st_size > 0 for name in assets))


if __name__ == "__main__":
    unittest.main()
