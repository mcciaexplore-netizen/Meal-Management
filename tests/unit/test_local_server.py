import base64
import contextlib
import io
import os
import secrets
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from meal_management.local_server import PROJECT_ROOT, _environment, main


def environment():
    return {
        "APP_ENV": "development", "APP_ORIGIN": "http://localhost:8000",
        "SCANNER_ORIGIN": "http://localhost:8001", "ALLOWED_HOSTS": "localhost,127.0.0.1",
        "APP_CSRF_SECRET": secrets.token_urlsafe(32), "LOGIN_RATE_SECRET": secrets.token_urlsafe(32),
        "DB_HOST": "database.invalid", "DB_NAME": "fictional_startup_test", "DB_USER": "fictional_user",
        "DB_PASSWORD": secrets.token_urlsafe(32),
        "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }


class LocalServerTests(unittest.TestCase):
    def test_file_only_ignores_exported_database_and_email_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "selected.env"
            path.write_text("DB_HOST=selected.invalid\nEMAIL_SEND_ENABLED=false\nDB_PASSWORD=literal${DB_HOST}\n")
            with patch.dict(os.environ, {"DB_HOST": "stale.invalid", "EMAIL_SEND_ENABLED": "true"}):
                before = dict(os.environ)
                selected = _environment(path, file_only=True)
                inherited = _environment(path)
                self.assertEqual(dict(os.environ), before)
        self.assertEqual(selected["DB_HOST"], "selected.invalid")
        self.assertEqual(selected["EMAIL_SEND_ENABLED"], "false")
        self.assertEqual(selected["DB_PASSWORD"], "literal${DB_HOST}")
        self.assertEqual(inherited["DB_HOST"], "stale.invalid")
        self.assertEqual(inherited["EMAIL_SEND_ENABLED"], "true")

    def test_both_launchers_pass_selected_file_without_inherited_settings(self):
        for name in ("admin", "scanner"):
            with self.subTest(application=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "aiven.env"
                values = environment()
                path.write_text("\n".join(key + "=" + value for key, value in values.items()))
                listener = Mock()
                port = 8000 if name == "admin" else 8001
                listener.getsockname.return_value = ("127.0.0.1", port)
                with (
                    patch.dict(os.environ, {"DB_HOST": "stale.invalid", "EMAIL_SEND_ENABLED": "true"}),
                    patch("meal_management.local_server._validate_assets"),
                    patch("meal_management.local_server.socket.socket", return_value=listener),
                    patch("meal_management.local_server._application") as application,
                    patch("uvicorn.Config"),
                    patch("uvicorn.Server") as server,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    server.return_value.started = True
                    self.assertEqual(main([name, "--env-file", str(path), "--env-file-only"]), 0)
                settings, runtime = application.call_args.args[1:]
                self.assertEqual(settings.db_host, values["DB_HOST"])
                self.assertFalse(runtime.email_send_enabled)
                listener.bind.assert_called_once_with(("127.0.0.1", port))

    def test_missing_environment_file_fails_before_binding_or_loading_application(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("meal_management.local_server.socket.socket") as listener:
                with patch("meal_management.local_server._application") as application:
                    with contextlib.redirect_stderr(io.StringIO()) as output:
                        result = main(["admin", "--env-file", str(Path(directory) / "missing.env")])
        self.assertEqual(result, 1)
        self.assertIn("Environment file is missing", output.getvalue())
        listener.assert_not_called()
        application.assert_not_called()

    def test_missing_configuration_names_are_clear_without_printing_values(self):
        values = {"DB_HOST": "fictional-private-host"}
        with patch("meal_management.local_server._environment", return_value=values):
            with patch("meal_management.local_server.socket.socket") as listener:
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    result = main(["admin"])
        self.assertEqual(result, 1)
        self.assertIn("DB_PASSWORD", output.getvalue())
        self.assertNotIn(values["DB_HOST"], output.getvalue())
        listener.assert_not_called()

    def test_launchers_use_distinct_defaults_and_only_loopback(self):
        for application, port in (("admin", 8000), ("scanner", 8001)):
            with self.subTest(application=application):
                listener = Mock()
                listener.getsockname.return_value = ("127.0.0.1", port)
                with (
                    patch("meal_management.local_server._environment", return_value=environment()),
                    patch("meal_management.local_server._validate_assets"),
                    patch("meal_management.local_server.socket.socket", return_value=listener),
                    patch("meal_management.local_server._application") as factory,
                    patch("uvicorn.Config") as config,
                    patch("uvicorn.Server") as server,
                    contextlib.redirect_stdout(io.StringIO()) as output,
                ):
                    server.return_value.started = True
                    self.assertEqual(main([application]), 0)
                listener.bind.assert_called_once_with(("127.0.0.1", port))
                server.return_value.run.assert_called_once_with(sockets=[listener])
                self.assertEqual(factory.call_args.args[0], application)
                self.assertFalse(config.call_args.kwargs["access_log"])
                self.assertFalse(config.call_args.kwargs["proxy_headers"])
                self.assertIn(f"http://localhost:{port}/", output.getvalue())
                listener.close.assert_called_once()

    def test_ephemeral_port_updates_only_selected_application_origin(self):
        values = environment()
        listener = Mock()
        listener.getsockname.return_value = ("127.0.0.1", 49123)
        with (
            patch("meal_management.local_server._environment", return_value=values),
            patch("meal_management.local_server._validate_assets"),
            patch("meal_management.local_server.socket.socket", return_value=listener),
            patch("meal_management.local_server._application") as factory,
            patch("uvicorn.Config"), patch("uvicorn.Server") as server,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            server.return_value.started = True
            self.assertEqual(main(["scanner", "--port", "0"]), 0)
        listener.bind.assert_called_once_with(("127.0.0.1", 0))
        runtime = factory.call_args.args[2]
        self.assertEqual(runtime.for_application("scanner").app_origin, "http://localhost:49123")
        self.assertEqual(runtime.for_application("admin").app_origin, "http://localhost:8000")

    def test_unexpected_factory_failure_never_prints_exception_secrets(self):
        listener = Mock()
        listener.getsockname.return_value = ("127.0.0.1", 8000)
        with (
            patch("meal_management.local_server._environment", return_value=environment()),
            patch("meal_management.local_server._validate_assets"),
            patch("meal_management.local_server.socket.socket", return_value=listener),
            patch("meal_management.local_server._application", side_effect=RuntimeError("fictional-secret-password")),
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(main(["admin"]), 1)
        self.assertNotIn("fictional-secret-password", output.getvalue())
        self.assertIn("could not start", output.getvalue())
        listener.close.assert_called_once()

    def test_remote_origin_is_rejected_before_opening_a_listener(self):
        values = {**environment(), "APP_ORIGIN": "http://remote.example.test:8000", "ALLOWED_HOSTS": "remote.example.test,localhost"}
        with patch("meal_management.local_server._environment", return_value=values):
            with patch("meal_management.local_server.socket.socket") as listener:
                with contextlib.redirect_stderr(io.StringIO()) as output:
                    self.assertEqual(main(["admin"]), 1)
        self.assertIn("Local application origins", output.getvalue())
        listener.assert_not_called()

    def test_missing_or_empty_application_assets_fail_before_listening(self):
        for application, bundle in (("admin", "app.js"), ("scanner", "scan-app.js")):
            for invalid in ("index.html", bundle):
                with self.subTest(application=application, invalid=invalid):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        assets = root / "frontend" / "dist" / application
                        assets.mkdir(parents=True)
                        for name in ("index.html", "styles.css", "scan-only.css", bundle):
                            (assets / name).write_text("asset" if name != invalid else "")
                        with (
                            patch("meal_management.local_server.PROJECT_ROOT", root),
                            patch("meal_management.local_server.os.chdir"),
                            patch("meal_management.local_server._environment", return_value=environment()),
                            patch("meal_management.local_server.socket.socket") as listener,
                            patch("meal_management.local_server._application") as factory,
                            contextlib.redirect_stderr(io.StringIO()) as output,
                        ):
                            self.assertEqual(main([application]), 1)
                        self.assertIn(application.capitalize() + " frontend assets are missing or incomplete", output.getvalue())
                        listener.assert_not_called()
                        factory.assert_not_called()

    def test_script_reports_missing_virtual_environment_from_any_directory(self):
        with tempfile.TemporaryDirectory(prefix="meal launcher ") as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            for name in ("start-admin.sh", "start-scanner.sh"):
                script = root / "deploy" / name
                script.write_text((PROJECT_ROOT / "deploy" / name).read_text())
                result = subprocess.run(["bash", str(script)], cwd="/", capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1)
                self.assertIn(".venv Python is missing", result.stderr)

    def test_scripts_resolve_real_project_without_reading_default_env(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.env")
            for name in ("start-admin.sh", "start-scanner.sh", "start-local.sh"):
                result = subprocess.run(
                    ["bash", str(PROJECT_ROOT / "deploy" / name), "--env-file", missing],
                    cwd=directory, capture_output=True, text=True, timeout=5,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("Environment file is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
