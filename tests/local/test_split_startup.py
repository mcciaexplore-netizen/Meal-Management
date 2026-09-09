import base64
import json
import os
import re
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
RUNNER = (
    "from unittest.mock import patch\n"
    "from meal_management.local_server import main\n"
    "with patch('meal_management.database.Database._connect', side_effect=AssertionError('DATABASE_ACCESS_FORBIDDEN')):\n"
    "    raise SystemExit(main())\n"
)


class SplitStartupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="meal-split-startup-")
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / "fictional.env"
        settings = {
            "APP_ENV": "development", "APP_ORIGIN": "http://localhost:8000",
            "SCANNER_ORIGIN": "http://localhost:8001", "ALLOWED_HOSTS": "localhost,127.0.0.1",
            "APP_CSRF_SECRET": secrets.token_urlsafe(32), "LOGIN_RATE_SECRET": secrets.token_urlsafe(32),
            "DB_HOST": "database.invalid", "DB_NAME": "fictional_startup_test", "DB_USER": "fictional_user",
            "DB_PASSWORD": secrets.token_urlsafe(32),
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "PHOTO_BACKEND": "local", "EMAIL_BACKEND": "preview", "EMAIL_SEND_ENABLED": "false",
            "PRIVATE_PHOTO_ROOT": str(Path(self.directory.name) / "photos"),
        }
        self.env_file.write_text("\n".join(key + "=" + value for key, value in settings.items()) + "\n")
        self.processes = []
        self.addCleanup(self.stop_all)

    def spawn(self, application, port=0):
        process = subprocess.Popen(
            [sys.executable, "-c", RUNNER, application, "--env-file", str(self.env_file), "--port", str(port)],
            cwd=self.directory.name,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(ROOT / "backend"), "PYTHONUNBUFFERED": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.processes.append(process)
        return process

    def started(self, application):
        process = self.spawn(application)
        output = bytearray()
        selected_port = None
        landing_url = None
        deadline = time.monotonic() + 15
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                for key, _ in selector.select(timeout=0.1):
                    chunk = key.fileobj.read1(65536)
                    if chunk:
                        output.extend(chunk)
                        match = re.search(rb"application: (http://localhost:(\d+)/)\s", output)
                        if match:
                            landing_url = match.group(1).decode()
                            selected_port = int(match.group(2))
                    elif process.poll() is not None:
                        self.fail("Local application exited during startup: " + output.decode(errors="replace"))
                if selected_port is not None:
                    try:
                        self.assertEqual(self.health(selected_port), {"status": "ok"})
                        return process, selected_port, landing_url
                    except (URLError, TimeoutError, ConnectionError):
                        pass
        self.fail("Local application did not become live: " + output.decode(errors="replace"))

    def health(self, port):
        request = Request("http://127.0.0.1:" + str(port) + "/health/live", headers={"Host": "localhost:" + str(port)})
        with urlopen(request, timeout=1) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def stop(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)

    def stop_all(self):
        for process in self.processes:
            self.stop(process)
            if process.stdout is not None:
                process.stdout.close()

    def test_admin_and_scanner_are_independent_without_database_access(self):
        admin, admin_port, admin_url = self.started("admin")
        scanner, scanner_port, scanner_url = self.started("scanner")
        self.assertNotEqual(admin.pid, scanner.pid)
        self.assertNotEqual(admin_port, scanner_port)
        self.assertEqual(self.health(admin_port), {"status": "ok"})
        self.assertEqual(self.health(scanner_port), {"status": "ok"})
        for url, bundle, foreign_bundle in (
            (admin_url, "app.js", "scan-app.js"),
            (scanner_url, "scan-app.js", "app.js"),
        ):
            with urlopen(url, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(("/assets/" + bundle).encode(), response.read())
            with urlopen(url + "assets/" + bundle, timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(len(response.read()), 0)
            with self.assertRaises(HTTPError) as denied:
                urlopen(url + "assets/" + foreign_bundle, timeout=2)
            self.assertEqual(denied.exception.code, 404)
            denied.exception.close()
        self.stop(admin)
        self.assertIsNotNone(admin.poll())
        self.assertIsNone(scanner.poll())
        self.assertEqual(self.health(scanner_port), {"status": "ok"})

    def test_occupied_port_fails_without_stopping_its_owner(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owner:
            owner.bind(("127.0.0.1", 0))
            owner.listen(1)
            port = owner.getsockname()[1]
            process = self.spawn("scanner", port)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 1)
            self.assertIn("Port " + str(port) + " is already in use", output.decode())
            self.assertEqual(owner.getsockname()[1], port)
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                connection, _ = owner.accept()
                connection.close()


if __name__ == "__main__":
    unittest.main()
