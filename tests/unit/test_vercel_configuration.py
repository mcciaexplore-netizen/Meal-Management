import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("meal_vercel_configuration", ROOT / "deploy/vercel/configure_projects.py")
configuration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configuration)


class VercelConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "prepared"
        self.directory.mkdir(mode=0o700)
        self.cli = self.root / "fictional-vercel"
        self.cli.write_bytes(b"not executable test code")
        self.cli.chmod(0o700)
        self.calls = []
        self.created = set()
        self.password = "fictional-private-database-password-00000000"
        self.values = dict(configuration.FORCED_VALUES, **{
            "APP_ORIGIN": "https://mccia-meal-admin.vercel.app", "SCANNER_ORIGIN": "https://mccia-meal-scanner.vercel.app",
            "ALLOWED_HOSTS": "mccia-meal-admin.vercel.app,mccia-meal-scanner.vercel.app",
            "APP_CSRF_SECRET": "a" * 32, "LOGIN_RATE_SECRET": "b" * 32, "LOGIN_WINDOW_SECONDS": "60",
            "DB_HOST": "fictional.aivencloud.com", "DB_PORT": "12345", "DB_PASSWORD": self.password,
            "DB_CONNECT_TIMEOUT": "10", "DB_SSL_CA_PEM": "-----BEGIN CERTIFICATE-----\nfictional\n-----END CERTIFICATE-----\n",
            "QR_ENCRYPTION_KEYS": base64.urlsafe_b64encode(b"k" * 32).decode(),
            "BLOB_STORE_HOST": "fictional.private.blob.vercel-storage.com", "EMAIL_SENDER": "sender@example.com",
            "GMAIL_APP_PASSWORD": "abcdefghijklmnop", "SCAN_REQUEST_LIMIT": "120", "SCAN_IP_LIMIT": "600", "SCAN_WINDOW_SECONDS": "60",
        })
        self.review = {
            "format_version": 1, "status": "prepared_offline", "scope": configuration.SCOPE,
            "projects": {role: {
                "name": name, "id": "prj_Fictional" + role.title() + "123456789",
                "domains": [name + ".vercel.app"], "payload_file": role + ".env.json",
            } for role, name in configuration.PROJECTS.items()},
            "origins": {"admin": self.values["APP_ORIGIN"], "scanner": self.values["SCANNER_ORIGIN"]},
            "database": {"host": self.values["DB_HOST"], "port": 12345, "name": "defaultdb", "user": "meal_runtime"},
            "blob_store_host": self.values["BLOB_STORE_HOST"], "blob_token_source": "existing_vercel_connection",
            "deployment_performed": False, "sending_enabled": False, "scanner_enabled": False,
        }
        self.rows = {}
        for role in configuration.PROJECTS:
            self.rows[role] = [{"key": key, "value": value, "type": "sensitive" if key in configuration.SENSITIVE_KEYS else "encrypted", "target": ["production"]} for key, value in sorted(dict(self.values, MEAL_APPLICATION=role).items())]
            self.write_payload(role)

    def write_review(self):
        path = self.directory / "review.json"
        path.write_text(json.dumps(self.review, sort_keys=True) + "\n")
        path.chmod(0o600)

    def write_payload(self, role):
        data = (json.dumps(self.rows[role], sort_keys=True) + "\n").encode()
        path = self.directory / (role + ".env.json")
        path.write_bytes(data)
        path.chmod(0o600)
        self.review["projects"][role]["sha256"] = hashlib.sha256(data).hexdigest()
        self.write_review()

    def blobs(self):
        return [{"key": key, "type": kind, "target": ["production"], "value": "never-print-private-provider-blob", "visibility": None} for key, kind in configuration.BLOB_TYPES.items()]

    def provider(self, arguments, **options):
        self.calls.append(arguments)
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertEqual(options["timeout"], 60)
        self.assertEqual(options["env"]["VERCEL_TELEMETRY_DISABLED"], "1")
        self.assertEqual(options["env"]["CI"], "1")
        self.assertEqual(options["cwd"], self.directory)
        self.assertNotIn("shell", options)
        self.assertNotIn(self.password, str(arguments))
        self.assertNotIn("--upsert", arguments)
        self.assertNotIn("--force", arguments)
        self.assertEqual(arguments[-3:], ["--scope", configuration.SCOPE, "--raw"])
        endpoint = arguments[2]
        role = "scanner" if "mccia-meal-scanner" in endpoint or self.review["projects"]["scanner"]["id"] in endpoint else "admin"
        method = arguments[arguments.index("--method") + 1]
        if method == "POST":
            self.assertIn(self.review["projects"][role]["id"], endpoint)
            self.assertEqual(arguments[arguments.index("--input") + 1], "-")
            self.assertEqual(arguments[arguments.index("--header") + 1], "Content-Type: application/json")
            self.assertNotIn("stdin", options)
            transmitted = json.loads(options["input"])
            self.assertIsInstance(transmitted, str)
            self.assertEqual(transmitted.encode("utf-8"), (self.directory / (role + ".env.json")).read_bytes())
            self.assertEqual(json.loads(transmitted), self.rows[role])
            self.created.add(role)
            response = {"created": self.rows[role], "failed": []}
        elif "/domains?" in endpoint:
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn("input", options)
            project = self.review["projects"][role]
            response = {"domains": [{"name": project["domains"][0], "projectId": project["id"], "verified": True}], "pagination": {"next": None}}
        else:
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn("input", options)
            self.assertTrue(endpoint.endswith("/env?decrypt=false"))
            response = {"envs": self.blobs() + (self.rows[role] if role in self.created else []), "pagination": {"next": None}}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(response).encode(), b"never-print-private-provider-error")

    def run_configuration(self, *, provider=None, allow=True, tty=True, confirmation=configuration.CONFIRMATION):
        with patch.object(configuration.sys.stdin, "isatty", return_value=tty), patch.object(configuration.ssl, "create_default_context"), patch.object(configuration.subprocess, "run", side_effect=provider or self.provider):
            return configuration.configure_projects(self.directory, allow_cloud_settings=allow, confirmation=confirmation, cli=self.cli)

    def test_permission_and_terminal_gates_precede_file_reads_and_provider_calls(self):
        for allow, tty, code in ((False, True, "EXPLICIT_APPROVAL_REQUIRED"), (True, False, "INTERACTIVE_TERMINAL_REQUIRED")):
            with self.subTest(code=code), patch.object(configuration, "load_configuration") as load:
                with self.assertRaisesRegex(configuration.ConfigurationError, code):
                    self.run_configuration(allow=allow, tty=tty)
                load.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_exact_confirmation_precedes_every_provider_request(self):
        with self.assertRaisesRegex(configuration.ConfigurationError, "CONFIRMATION_MISMATCH"):
            self.run_configuration(confirmation="yes")
        self.assertEqual(self.calls, [])

    def test_complete_run_preflights_both_projects_then_creates_and_reads_back_exact_metadata(self):
        originals = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = self.run_configuration()
        self.assertEqual(result["status"], "verified")
        self.assertEqual([row["name"] for row in result["projects"]], list(configuration.PROJECTS.values()))
        self.assertEqual([arguments[arguments.index("--method") + 1] for arguments in self.calls], ["GET", "GET", "GET", "GET", "POST", "GET", "POST", "GET"])
        for role in configuration.PROJECTS:
            self.assertFalse(set(row["key"] for row in self.rows[role]).intersection(configuration.BLOB_TYPES))
        self.assertNotIn(self.password, json.dumps(result))
        self.assertEqual({path.name for path in self.directory.iterdir()}, {"admin.env.json", "scanner.env.json", "review.json"})
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, originals)

    def test_installed_cli_stdin_envelope_preserves_array_body_and_certificate_newlines_offline(self):
        chunks = ROOT / "build/vercel-tools/node_modules/vercel/dist/chunks"
        api_file = chunks / "chunk-EGMJRU5P.js"
        client_file = chunks / "chunk-2PPXY24N.js"
        node = shutil.which("node")
        if not node or not api_file.is_file() or not client_file.is_file():
            self.skipTest("Installed Vercel 59.15.1 and Node are required for this offline transport regression")
        captured = []

        def capture(arguments, **options):
            captured.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, b"{}", b"")

        original = (self.directory / "admin.env.json").read_bytes()
        with patch.object(configuration.subprocess, "run", side_effect=capture):
            configuration._request(
                self.cli, self.directory, "/v10/projects/prj_FictionalAdmin123456789/env", method="POST",
                payload=self.directory / "admin.env.json", payload_sha256=hashlib.sha256(original).hexdigest(),
            )
        script = """
const fs = require('node:fs');
globalThis.fetch = () => { throw new Error('Network access is forbidden'); };
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const api = fs.readFileSync(input.apiFile, 'utf8');
const client = fs.readFileSync(input.clientFile, 'utf8');
const build = api.slice(api.indexOf('async function buildRequest('), api.indexOf('async function parseCliKeyValueField('));
const readStdin = api.slice(api.indexOf('async function readStdin('), api.indexOf('function formatOutput('));
const predicate = client.match(/isJSONObject=(v=>[^;]+);/)[1];
const start = client.indexOf('let body;isJSONObject(opts.body)');
const transport = client.slice(start, client.indexOf('let requestId=', start));
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
const parse = new AsyncFunction('flags', 'process', build + readStdin + ';return buildRequest("/offline", flags);');
const serialize = new Function('opts', 'const headers = new Headers(opts.headers); const isJSONObject=' + predicate + ';' + transport + ';return {body, headers};');
async function inspect(text) {
  const encoded = Buffer.from(text, 'utf8');
  const stdin = {async *[Symbol.asyncIterator]() {for (let i=0;i<encoded.length;i+=7) yield encoded.subarray(i,i+7);}};
  const config = await parse({'--input':'-', '--method':'POST', '--header':['Content-Type: application/json']}, {stdin});
  const request = new Request('https://example.invalid/offline-only', {method:config.method, ...serialize(config)});
  return {contentType:request.headers.get('content-type'), body:await request.text(), parsedType:typeof config.body};
}
(async () => process.stdout.write(JSON.stringify({original:await inspect(input.original), corrected:await inspect(input.envelope)})))().catch(() => process.exit(1));
"""
        result = subprocess.run(
            [node, "-e", script], input=json.dumps({
                "apiFile": str(api_file), "clientFile": str(client_file),
                "original": original.decode("utf-8"), "envelope": captured[0][1]["input"].decode("utf-8"),
            }).encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["original"]["body"], ",".join("[object Object]" for row in self.rows["admin"]))
        self.assertEqual(observed["corrected"]["parsedType"], "string")
        self.assertEqual(observed["corrected"]["contentType"], "application/json")
        self.assertEqual(observed["corrected"]["body"].encode("utf-8"), original)
        restored = {row["key"]: row["value"] for row in json.loads(observed["corrected"]["body"])}
        self.assertEqual(restored["DB_SSL_CA_PEM"], self.values["DB_SSL_CA_PEM"])
        self.assertNotIn(self.password, str(captured[0][0]))

    def test_payload_checksum_is_rechecked_before_stdin_serialization(self):
        path = self.directory / "admin.env.json"
        path.write_bytes(path.read_bytes() + b" ")
        with patch.object(configuration.subprocess, "run") as run:
            with self.assertRaisesRegex(configuration.ConfigurationError, "PREPARED_FILES_CHANGED"):
                configuration._request(
                    self.cli, self.directory, "/offline", method="POST", payload=path,
                    payload_sha256=self.review["projects"]["admin"]["sha256"],
                )
            run.assert_not_called()

    def test_payload_files_and_directories_must_be_private_and_not_symlinked(self):
        path = self.directory / "admin.env.json"
        path.chmod(0o644)
        with self.assertRaisesRegex(configuration.ConfigurationError, "PRIVATE_FILE_REQUIRED"):
            self.run_configuration()
        path.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(configuration.ConfigurationError, "PRIVATE_DIRECTORY_REQUIRED"):
            self.run_configuration()
        self.directory.chmod(0o700)
        original = path.read_bytes()
        other = self.root / "linked-content"
        other.write_bytes(original)
        other.chmod(0o600)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaisesRegex(configuration.ConfigurationError, "UNSAFE_PATH"):
            self.run_configuration()
        self.assertEqual(self.calls, [])

    def test_changed_checksum_role_and_missing_project_id_fail_before_provider_access(self):
        path = self.directory / "admin.env.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(configuration.ConfigurationError, "CHECKSUM_MISMATCH"):
            self.run_configuration()
        self.write_payload("admin")
        role = next(row for row in self.rows["scanner"] if row["key"] == "MEAL_APPLICATION")
        role["value"] = "admin"
        self.write_payload("scanner")
        with self.assertRaisesRegex(configuration.ConfigurationError, "APPLICATION_MISMATCH"):
            self.run_configuration()
        role["value"] = "scanner"
        self.write_payload("scanner")
        self.review["projects"]["scanner"]["id"] = None
        self.write_review()
        with self.assertRaisesRegex(configuration.ConfigurationError, "REVIEW_PROJECT_MISMATCH"):
            self.run_configuration()
        self.assertEqual(self.calls, [])

    def test_unreviewed_payload_keys_targets_and_sensitive_types_are_rejected(self):
        original = copy.deepcopy(self.rows["admin"])
        alterations = [
            lambda rows: rows.append({"key": "BLOB_READ_WRITE_TOKEN", "value": "private", "type": "sensitive", "target": ["production"]}),
            lambda rows: rows[0].update(target=["preview"]),
            lambda rows: next(row for row in rows if row["key"] == "DB_PASSWORD").update(type="encrypted"),
            lambda rows: next(row for row in rows if row["key"] == "EMAIL_SEND_ENABLED").update(value="true"),
            lambda rows: next(row for row in rows if row["key"] == "SCAN_APP_ENABLED").update(value="true"),
        ]
        for alter in alterations:
            self.rows["admin"] = copy.deepcopy(original)
            alter(self.rows["admin"])
            self.write_payload("admin")
            with self.assertRaises(configuration.ConfigurationError):
                self.run_configuration()
        self.assertEqual(self.calls, [])

    def test_shared_secret_mismatch_and_missing_ca_fail_locally(self):
        secret = next(row for row in self.rows["scanner"] if row["key"] == "APP_CSRF_SECRET")
        secret["value"] = "z" * 32
        self.write_payload("scanner")
        with self.assertRaisesRegex(configuration.ConfigurationError, "SHARED_CONFIGURATION_MISMATCH"):
            self.run_configuration()
        secret["value"] = self.values["APP_CSRF_SECRET"]
        ca = next(row for row in self.rows["scanner"] if row["key"] == "DB_SSL_CA_PEM")
        ca["value"] = "not a CA certificate"
        self.write_payload("scanner")
        with self.assertRaisesRegex(configuration.ConfigurationError, "VALID_DATABASE_CA_REQUIRED"):
            self.run_configuration()
        self.assertEqual(self.calls, [])

    def test_second_project_preflight_rejects_existing_application_variables_before_any_write(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            if "mccia-meal-scanner/env?" in arguments[2]:
                body = json.loads(result.stdout)
                body["envs"].append({"key": "APP_ENV", "type": "encrypted", "target": ["production"]})
                result.stdout = json.dumps(body).encode()
            return result

        with self.assertRaisesRegex(configuration.ConfigurationError, "EXISTING_VARIABLES_REQUIRE_REVIEW") as failure:
            self.run_configuration(provider=provider)
        self.assertFalse(failure.exception.changes_possible)
        self.assertFalse(self.created)
        self.assertTrue(all("POST" not in call for call in self.calls))

    def test_missing_sensitive_production_blob_variables_are_rejected(self):
        for change in (lambda rows: rows.pop(), lambda rows: rows[0].update(type="plain"), lambda rows: rows[0].update(target=["production", "preview"]), lambda rows: rows[0].update(gitBranch="feature")):
            def provider(arguments, **options):
                result = self.provider(arguments, **options)
                body = json.loads(result.stdout)
                change(body["envs"])
                result.stdout = json.dumps(body).encode()
                return result

            with self.assertRaises(configuration.ConfigurationError):
                self.run_configuration(provider=provider)
        self.assertFalse(self.created)

    def test_domains_require_reviewed_project_id_verified_origin_and_no_pagination(self):
        for change in (lambda body: body["domains"][0].update(projectId="prj_OtherProject123456"), lambda body: body["domains"][0].update(verified=False), lambda body: body.update(pagination={"next": 123})):
            def provider(arguments, **options):
                result = self.provider(arguments, **options)
                if "/domains?" in arguments[2]:
                    body = json.loads(result.stdout)
                    change(body)
                    result.stdout = json.dumps(body).encode()
                return result

            with self.assertRaises(configuration.ConfigurationError):
                self.run_configuration(provider=provider)
        self.assertFalse(self.created)

    def test_populated_standard_visibility_metadata_is_accepted(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            body = json.loads(result.stdout)
            for row in body.get("envs", body.get("created", [])):
                row["visibility"] = "secret" if row["type"] == "sensitive" else "config"
            result.stdout = json.dumps(body).encode()
            return result

        self.assertEqual(self.run_configuration(provider=provider)["status"], "verified")

    def test_partial_post_response_stops_without_retry_or_readback_and_marks_ambiguity(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            if "POST" in arguments:
                result.stdout = json.dumps({"created": self.rows["admin"][:1], "failed": [{"error": self.password}]}).encode()
            return result

        with self.assertRaisesRegex(configuration.ConfigurationError, "PARTIAL_OR_UNCONFIRMED") as failure:
            self.run_configuration(provider=provider)
        self.assertTrue(failure.exception.changes_possible)
        self.assertEqual(failure.exception.verified, ())
        self.assertEqual(len(self.calls), 5)
        self.assertNotIn(self.password, str(failure.exception))

    def test_scanner_post_failure_preserves_verified_admin_state(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            if "POST" in arguments and self.review["projects"]["scanner"]["id"] in arguments[2]:
                result.returncode = 1
                result.stdout = self.password.encode()
                result.stderr = self.password.encode()
            return result

        with self.assertRaisesRegex(configuration.ConfigurationError, "PROVIDER_REQUEST_FAILED") as failure:
            self.run_configuration(provider=provider)
        self.assertTrue(failure.exception.changes_possible)
        self.assertEqual(failure.exception.verified, (configuration.PROJECTS["admin"],))
        self.assertEqual(len(self.calls), 7)
        self.assertNotIn(self.password, str(failure.exception))

    def test_post_timeout_is_ambiguous_and_does_not_retry(self):
        def provider(arguments, **options):
            if "POST" in arguments:
                self.calls.append(arguments)
                raise subprocess.TimeoutExpired(arguments, 60, output=self.password.encode(), stderr=self.password.encode())
            return self.provider(arguments, **options)

        with self.assertRaisesRegex(configuration.ConfigurationError, "PROVIDER_TIMEOUT") as failure:
            self.run_configuration(provider=provider)
        self.assertTrue(failure.exception.changes_possible)
        self.assertEqual(len(self.calls), 5)
        self.assertNotIn(self.password, str(failure.exception))

    def test_readback_mismatch_stops_before_scanner_write(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            if "admin" in self.created and arguments[2].endswith("env?decrypt=false"):
                body = json.loads(result.stdout)
                body["envs"].pop()
                result.stdout = json.dumps(body).encode()
            return result

        with self.assertRaisesRegex(configuration.ConfigurationError, "READBACK_MISMATCH") as failure:
            self.run_configuration(provider=provider)
        self.assertTrue(failure.exception.changes_possible)
        self.assertEqual(len(self.calls), 6)
        self.assertEqual(self.created, {"admin"})

    def test_changed_payload_during_preflight_is_detected_before_first_write(self):
        def provider(arguments, **options):
            result = self.provider(arguments, **options)
            if "scanner/domains" in arguments[2]:
                path = self.directory / "admin.env.json"
                path.write_bytes(path.read_bytes() + b" ")
            return result

        with self.assertRaisesRegex(configuration.ConfigurationError, "PREPARED_FILES_CHANGED"):
            self.run_configuration(provider=provider)
        self.assertFalse(self.created)

    def test_cli_prints_only_safe_status_and_preserves_partial_change_guidance(self):
        error = configuration.ConfigurationError("VERCEL_SETTINGS_PROVIDER_REQUEST_FAILED", changes_possible=True, verified=[configuration.PROJECTS["admin"]])
        error.private_response = self.password
        output = io.StringIO()
        errors = io.StringIO()
        with patch.object(configuration, "configure_projects", side_effect=error), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = configuration.main(["--directory", str(self.directory), "--allow-cloud-settings"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("mccia-meal-admin", errors.getvalue())
        self.assertIn("Do not rerun", errors.getvalue())
        self.assertNotIn(self.password, errors.getvalue())


if __name__ == "__main__":
    unittest.main()
