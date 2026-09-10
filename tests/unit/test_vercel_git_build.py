import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("meal_vercel_git_build_tests", ROOT / "deploy" / "vercel" / "build_git.py")
git_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(git_build)
packaging = git_build.packaging
SCAFFOLD = ("app.py", "pyproject.toml", ".python-version", "vercel.json", ".gitignore")


class VercelGitBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        backend = self.root / "backend" / "meal_management"
        backend.mkdir(parents=True)
        for name in packaging.RUNTIME_MODULES:
            (backend / (name + ".py")).write_text("VALUE = " + repr(name) + "\n", encoding="utf-8")
        (self.root / "pyproject.toml").write_text(
            '[project]\nname="fictional-meal-app"\ndependencies=["fastapi>=0.115,<1"]\n'
            '[project.optional-dependencies]\nvercel=["vercel>=0.5,<1", "httpx>=0.28,<1"]\n',
            encoding="utf-8",
        )
        self.scaffold = {}
        for role in ("admin", "scanner"):
            target = self.target(role)
            target.mkdir(parents=True)
            (target / "app.py").write_bytes(packaging._entry(role))
            (target / ".python-version").write_text("3.12\n", encoding="ascii")
            (target / "pyproject.toml").write_text(
                '[project]\nname="fictional-meal-' + role + '"\nversion="0.1.0"\n'
                'requires-python=">=3.12,<3.13"\n'
                'dependencies=["fastapi>=0.115,<1", "vercel>=0.5,<1", "httpx>=0.28,<1"]\n'
                '[tool.uv]\npackage=false\n[tool.vercel]\nentrypoint="app:app"\n'
                '[tool.vercel.fastapi.static]\ncdn=false\n',
                encoding="utf-8",
            )
            (target / "vercel.json").write_text('{"fixture":"tracked-configuration"}\n', encoding="utf-8")
            (target / ".gitignore").write_text("backend/\nfrontend/\npublic/\ngit-build-manifest.json\n", encoding="utf-8")
            self.scaffold[role] = {name: (target / name).read_bytes() for name in SCAFFOLD}
        for name in (".env", "var/private/photo.png", "backend/meal_management/runtime_account.py"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("FICTIONAL_PRIVATE_MARKER", encoding="ascii")
        self.frontend = patch.object(packaging, "_build_frontend", side_effect=self.build_frontend).start()
        self.addCleanup(patch.stopall)

    def target(self, role="admin"):
        return self.root / "apps" / role

    def build_frontend(self, project_root, output):
        self.assertEqual(Path(project_root), self.root)
        for role, assets in packaging.ASSETS.items():
            directory = output / role
            directory.mkdir(parents=True)
            for name in (*assets, "index.html"):
                (directory / name).write_text(role + " " + name + "\n", encoding="utf-8")

    def build(self, role="admin"):
        return git_build.build_git(role, project_root=self.root)

    def snapshot(self, role="admin"):
        result = {}
        for path in sorted(self.target(role).rglob("*")):
            relative = path.relative_to(self.target(role)).as_posix()
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", path.read_bytes())
            elif path.is_dir():
                result[relative] = ("directory",)
        return result

    def manifest(self, role="admin"):
        return json.loads((self.target(role) / "git-build-manifest.json").read_text(encoding="utf-8"))

    def expected_files(self, role="admin"):
        names = {"backend/meal_management/" + name + ".py" for name in packaging.RUNTIME_MODULES}
        names.update("frontend/dist/" + role + "/" + name for name in (*packaging.ASSETS[role], "index.html"))
        names.update("public/assets/" + name for name in packaging.ASSETS[role])
        return names

    def assert_unchanged_failure(self, role="admin"):
        before = self.snapshot(role)
        with self.assertRaises(git_build.GitBuildError):
            self.build(role)
        self.assertEqual(self.snapshot(role), before)

    def test_builds_each_role_with_exact_allowlist_and_preserves_scaffold(self):
        for role in ("admin", "scanner"):
            with self.subTest(role=role):
                summary = self.build(role)
                self.assertEqual(summary["application"], role)
                self.assertEqual(summary["file_count"], len(self.expected_files(role)))
                self.assertEqual(set(summary), {"application", "file_count", "bytes"})
                self.assertGreater(summary["bytes"], 0)
                manifest = self.manifest(role)
                self.assertEqual(set(manifest), {"format_version", "application", "files"})
                self.assertEqual(manifest["format_version"], 1)
                self.assertEqual(manifest["application"], role)
                self.assertEqual(set(manifest["files"]), self.expected_files(role))
                for name, checksum in manifest["files"].items():
                    data = (self.target(role) / name).read_bytes()
                    self.assertEqual(checksum, hashlib.sha256(data).hexdigest())
                    self.assertNotIn(b"FICTIONAL_PRIVATE_MARKER", data)
                for name, original in self.scaffold[role].items():
                    self.assertEqual((self.target(role) / name).read_bytes(), original)

    def test_roles_have_separate_frontend_and_public_assets(self):
        for role in ("admin", "scanner"):
            self.build(role)
            target = self.target(role)
            self.assertEqual({path.name for path in (target / "frontend" / "dist").iterdir()}, {role})
            self.assertEqual({path.name for path in (target / "public" / "assets").iterdir()}, set(packaging.ASSETS[role]))
            self.assertFalse((target / "public" / "index.html").exists())
            self.assertFalse((target / "backend" / "meal_management" / "runtime_account.py").exists())

    def test_repeat_unchanged_build_is_deterministic(self):
        first = self.build()
        before = self.snapshot()
        second = self.build()
        self.assertEqual(first, second)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.frontend.call_count, 2)

    def test_repeat_can_update_changed_source_when_previous_outputs_match_manifest(self):
        self.build()
        name = "backend/meal_management/runtime.py"
        previous = self.manifest()["files"][name]
        changed = b"VALUE = 'reviewed revision'\n"
        (self.root / name).write_bytes(changed)
        self.build()
        self.assertEqual((self.target() / name).read_bytes(), changed)
        self.assertNotEqual(self.manifest()["files"][name], previous)
        for filename, original in self.scaffold["admin"].items():
            self.assertEqual((self.target() / filename).read_bytes(), original)

    def test_tampered_generated_file_refuses_repeat_without_overwrite(self):
        self.build()
        path = self.target() / "backend" / "meal_management" / "runtime.py"
        path.write_text("USER_CHANGE = 'preserve this'\n", encoding="utf-8")
        self.frontend.reset_mock()
        self.assert_unchanged_failure()
        self.frontend.assert_not_called()

    def test_unknown_generated_file_refuses_repeat_without_deletion(self):
        self.build()
        path = self.target() / "public" / "assets" / "unexpected.txt"
        path.write_text("preserve unrelated output", encoding="utf-8")
        self.assert_unchanged_failure()

    def test_initial_unowned_generated_file_is_not_replaced(self):
        path = self.target() / "backend" / "unrelated.py"
        path.parent.mkdir()
        path.write_text("USER_WORK = True\n", encoding="utf-8")
        self.assert_unchanged_failure()
        self.assertFalse((self.target() / "git-build-manifest.json").exists())

    def test_initial_known_empty_generated_directories_are_supported(self):
        for directory in ("backend/meal_management", "frontend/dist/admin", "public/assets"):
            (self.target() / directory).mkdir(parents=True)
        self.build()
        self.assertEqual(set(self.manifest()["files"]), self.expected_files())

    def test_initial_unknown_empty_generated_directory_is_refused(self):
        (self.target() / "backend" / "unrelated").mkdir(parents=True)
        self.assert_unchanged_failure()

    def test_output_symlink_is_refused_and_target_is_preserved(self):
        self.build()
        path = self.target() / "backend" / "meal_management" / "runtime.py"
        destination = self.root / "preserved.py"
        destination.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(destination)
        original = destination.read_bytes()
        self.assert_unchanged_failure()
        self.assertEqual(destination.read_bytes(), original)

    def test_output_hardlink_is_refused_and_link_is_preserved(self):
        self.build()
        path = self.target() / "backend" / "meal_management" / "runtime.py"
        alias = self.root / "preserved-link.py"
        os.link(path, alias)
        original = alias.read_bytes()
        self.assert_unchanged_failure()
        self.assertEqual(alias.read_bytes(), original)

    def test_runtime_source_symlink_is_refused_before_changing_output(self):
        self.build()
        source = self.root / "backend" / "meal_management" / "runtime.py"
        destination = self.root / "source-real.py"
        destination.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(destination)
        self.assert_unchanged_failure()

    def test_runtime_source_hardlink_is_refused_before_changing_output(self):
        self.build()
        source = self.root / "backend" / "meal_management" / "runtime.py"
        os.link(source, self.root / "source-alias.py")
        self.assert_unchanged_failure()

    def test_runtime_source_import_outside_allowlist_refuses_output_changes(self):
        self.build()
        source = self.root / "backend" / "meal_management" / "runtime.py"
        source.write_text("from .runtime_account import ACCOUNT\n", encoding="utf-8")
        self.assert_unchanged_failure()

    def test_role_entrypoint_mismatch_is_rejected(self):
        (self.target() / "app.py").write_bytes(packaging._entry("scanner"))
        self.assert_unchanged_failure()
        self.frontend.assert_not_called()

    def test_role_dependency_mismatch_is_rejected(self):
        path = self.target() / "pyproject.toml"
        content = path.read_text()
        path.write_text(content.replace('"vercel>=0.5,<1", ', ""), encoding="utf-8")
        self.assert_unchanged_failure()
        self.frontend.assert_not_called()

    def test_runtime_import_and_static_settings_are_required(self):
        path = self.target() / "pyproject.toml"
        original = path.read_text()
        for old, new in (("cdn=false", "cdn=true"), ('entrypoint="app:app"', 'entrypoint="other:app"'), ("package=false", "package=true")):
            with self.subTest(setting=old):
                path.write_text(original.replace(old, new), encoding="utf-8")
                self.assert_unchanged_failure()
        path.write_text(original, encoding="utf-8")

    def test_wrong_python_version_is_refused(self):
        (self.target() / ".python-version").write_text("3.11\n", encoding="ascii")
        self.assert_unchanged_failure()
        self.frontend.assert_not_called()

    def test_corrupt_or_wrong_role_manifest_refuses_repeat_without_changes(self):
        self.build()
        path = self.target() / "git-build-manifest.json"
        original = self.manifest()
        variants = (
            "not json",
            json.dumps(dict(original, application="scanner")),
            json.dumps(dict(original, files={"../outside": "a" * 64})),
            json.dumps(dict(original, unexpected="value")),
        )
        for variant in variants:
            with self.subTest(variant=variant[:40]):
                path.write_text(variant, encoding="utf-8")
                self.assert_unchanged_failure()

    def test_missing_output_from_existing_manifest_is_not_silently_rebuilt(self):
        self.build()
        (self.target() / "public" / "assets" / "app.js").unlink()
        self.assert_unchanged_failure()

    def test_frontend_failure_preserves_previously_generated_outputs(self):
        self.build()
        self.frontend.side_effect = RuntimeError("fictional-sensitive-build-detail")
        before = self.snapshot()
        with self.assertRaises(git_build.GitBuildError) as caught:
            self.build()
        self.assertNotIn("fictional-sensitive-build-detail", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_unknown_application_is_rejected_before_building(self):
        with self.assertRaises(git_build.GitBuildError):
            self.build("visitor")
        self.frontend.assert_not_called()

    def test_success_stdout_does_not_include_runtime_source_or_paths(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = self.build()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertNotIn("FICTIONAL_PRIVATE_MARKER", json.dumps(result))
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_publish_failure_restores_every_previous_generated_file_and_marker(self):
        self.build()
        before = self.snapshot()
        source = self.root / "backend" / "meal_management" / "runtime.py"
        source.write_text("VALUE = 'new reviewed source'\n", encoding="utf-8")
        original = Path.rename
        failed = False

        def rename(path, destination):
            nonlocal failed
            if not failed and path.name == "public" and path.parent.name == "package" and Path(destination) == self.target() / "public":
                failed = True
                raise OSError("fictional-sensitive-publish-detail")
            return original(path, destination)

        with patch.object(Path, "rename", new=rename):
            with self.assertRaises(git_build.GitBuildError) as caught:
                self.build()
        self.assertTrue(failed)
        self.assertNotIn("fictional-sensitive-publish-detail", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.target() / ".git-build.lock").exists())

    def test_existing_build_lock_is_preserved_and_stops_before_frontend(self):
        lock = self.target() / ".git-build.lock"
        lock.write_text("preserve existing builder lock", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(git_build.GitBuildError, "VERCEL_GIT_BUILD_IN_PROGRESS"):
            self.build()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock.read_text(), "preserve existing builder lock")
        self.frontend.assert_not_called()

    def test_main_sanitizes_unexpected_error_output(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(git_build, "build_git", side_effect=RuntimeError("fictional-sensitive-error-detail")), redirect_stdout(output), redirect_stderr(errors):
            status = git_build.main(["admin"])
        self.assertEqual(status, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "VERCEL_GIT_BUILD_FAILED\n")

    def test_environment_role_mismatch_precedes_filesystem_and_frontend(self):
        with patch.dict(os.environ, {"MEAL_APPLICATION": "scanner"}), patch.object(git_build, "_scaffold") as scaffold:
            with self.assertRaisesRegex(git_build.GitBuildError, "VERCEL_GIT_APPLICATION_MISMATCH"):
                self.build("admin")
        scaffold.assert_not_called()
        self.frontend.assert_not_called()
