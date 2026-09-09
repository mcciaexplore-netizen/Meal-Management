import base64
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from meal_management.delivery import LocalEmailPreview, SESDelivery, delivery_from_settings
from meal_management.email_queue import EmailPreview
from meal_management.errors import ConfigurationError, DomainError
from meal_management.runtime import RuntimeSettings
from meal_management.storage import LocalFileStorage, S3Storage, storage_from_settings


def environment(**overrides):
    values = {
        "APP_CSRF_SECRET": "base64:" + base64.b64encode(b"a" * 32).decode(),
        "LOGIN_RATE_SECRET": "base64:" + base64.b64encode(b"b" * 32).decode(),
    }
    values.update(overrides)
    return values


def png_photo():
    from PIL import Image, PngImagePlugin
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Employee", "Sensitive metadata")
    Image.new("RGB", (12, 12), "red").save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


class RuntimeSettingsTests(unittest.TestCase):
    def test_development_defaults_require_no_aws(self):
        settings = RuntimeSettings.from_env(environment())
        self.assertEqual(settings.environment, "development")
        self.assertEqual(settings.photo_backend, "local")
        self.assertEqual(settings.email_backend, "preview")
        self.assertFalse(settings.email_send_enabled)
        self.assertEqual(settings.csrf_secret, b"a" * 32)

    def test_missing_setting_identifies_only_name(self):
        with self.assertRaisesRegex(ConfigurationError, "MISSING_SETTING_APP_CSRF_SECRET"):
            RuntimeSettings.from_env({})

    def test_application_profiles_have_independent_origins_and_shared_settings(self):
        settings = RuntimeSettings.from_env(environment())
        admin = settings.for_application("admin")
        scanner = settings.for_application("scanner")
        self.assertEqual(admin.app_origin, "http://localhost:8000")
        self.assertEqual(scanner.app_origin, "http://localhost:8001")
        self.assertEqual(scanner.for_application("admin"), admin)
        self.assertEqual(scanner.for_application("scanner"), scanner)
        self.assertEqual(settings.app_origin, "http://localhost:8000")
        self.assertEqual(scanner.csrf_secret, admin.csrf_secret)
        self.assertEqual(scanner.photo_root, admin.photo_root)

    def test_scanner_origin_is_canonical_and_cannot_share_admin_origin(self):
        settings = RuntimeSettings.from_env(environment(SCANNER_ORIGIN="http://LOCALHOST:8001/"))
        self.assertEqual(settings.for_application("scanner").app_origin, "http://localhost:8001")
        same = RuntimeSettings.from_env(environment(SCANNER_ORIGIN="http://localhost:8000/"))
        with self.assertRaisesRegex(ConfigurationError, "SCANNER_ORIGIN_MUST_DIFFER"):
            same.for_application("scanner")

    def test_scanner_origin_rejects_invalid_urls_without_echoing_them(self):
        for origin in ("https://user:secret@example.test", "http://localhost:8001/path", "https://", "http://localhost:0"):
            with self.subTest(origin=origin):
                with self.assertRaises(ConfigurationError) as caught:
                    RuntimeSettings.from_env(environment(SCANNER_ORIGIN=origin))
                self.assertEqual(str(caught.exception), "INVALID_SETTING_SCANNER_ORIGIN")

    def test_scanner_profile_checks_its_host_and_secure_cookie_configuration(self):
        settings = RuntimeSettings.from_env(environment(
            SCANNER_ORIGIN="https://scanner.example.test", ALLOWED_HOSTS="localhost",
        ))
        with self.assertRaisesRegex(ConfigurationError, "INVALID_SETTING_ALLOWED_HOSTS"):
            settings.for_application("scanner")
        settings = RuntimeSettings.from_env(environment(
            APP_ORIGIN="https://localhost:8000", COOKIE_SECURE="true",
        ))
        with self.assertRaisesRegex(ConfigurationError, "COOKIE_SECURE_REQUIRES_HTTPS_SCANNER_ORIGIN"):
            settings.for_application("scanner")

    def test_application_profile_rejects_unknown_application(self):
        with self.assertRaisesRegex(ConfigurationError, "INVALID_APPLICATION"):
            RuntimeSettings.from_env(environment()).for_application("unknown")

    def test_short_secret_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "APP_CSRF_SECRET"):
            RuntimeSettings.from_env(environment(APP_CSRF_SECRET="short"))

    def test_malformed_base64_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "APP_CSRF_SECRET"):
            RuntimeSettings.from_env(environment(APP_CSRF_SECRET="base64:not base64"))

    def test_secrets_hidden_from_representations(self):
        settings = RuntimeSettings.from_env(environment())
        self.assertNotIn("a" * 32, repr(settings))
        self.assertNotIn("b" * 32, repr(settings))

    def test_origin_is_canonical(self):
        settings = RuntimeSettings.from_env(environment(APP_ORIGIN="http://LOCALHOST:80/"))
        self.assertEqual(settings.app_origin, "http://localhost")

    def test_invalid_origins_rejected(self):
        for origin in (
            "https://user:secret@example.com", "https://example.com/path",
            "https://example.com?secret=yes", "https://example.com/#fragment",
            "ftp://example.com", "https://", "http://example.com:0",
            "http://example.com:invalid", "http://example.com:99999",
        ):
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ConfigurationError, "APP_ORIGIN"):
                    RuntimeSettings.from_env(environment(APP_ORIGIN=origin))

    def test_hosts_require_origin_and_no_wildcards(self):
        for hosts in ("*", "example.com", "localhost,", "localhost:8000/path"):
            with self.subTest(hosts=hosts):
                with self.assertRaisesRegex(ConfigurationError, "ALLOWED_HOSTS"):
                    RuntimeSettings.from_env(environment(ALLOWED_HOSTS=hosts))

    def test_production_rejects_http(self):
        with self.assertRaisesRegex(ConfigurationError, "PRODUCTION_REQUIRES_HTTPS"):
            RuntimeSettings.from_env(environment(APP_ENV="production"))

    def test_production_rejects_development_adapters(self):
        with self.assertRaisesRegex(ConfigurationError, "PRODUCTION_REQUIRES_S3"):
            RuntimeSettings.from_env(environment(APP_ENV="production", APP_ORIGIN="https://meal.example.com"))

    def test_production_requires_explicit_aws_configuration(self):
        with self.assertRaisesRegex(ConfigurationError, "MISSING_SETTING_AWS_REGION"):
            RuntimeSettings.from_env(environment(
                APP_ENV="production", APP_ORIGIN="https://meal.example.com",
                PHOTO_BACKEND="s3", EMAIL_BACKEND="ses",
            ))

    def test_production_configuration_has_no_network_effect(self):
        settings = RuntimeSettings.from_env(environment(
            APP_ENV="production", APP_ORIGIN="https://meal.example.com",
            PHOTO_BACKEND="s3", EMAIL_BACKEND="ses", AWS_REGION="ap-south-1",
            PHOTO_S3_BUCKET="example-private-photos", EMAIL_SENDER="meals@example.com",
        ))
        self.assertEqual(settings.session_cookie, "__Host-meal_session")
        self.assertTrue(settings.cookie_secure)
        self.assertIsInstance(storage_from_settings(settings), S3Storage)
        self.assertIsInstance(delivery_from_settings(settings), SESDelivery)

    def test_invalid_configuration_bounds(self):
        for name, value in (
            ("SESSION_IDLE_MINUTES", "0"), ("LOGIN_ATTEMPT_LIMIT", "-1"),
            ("MAX_PHOTO_BYTES", "500"), ("LOGIN_IP_ATTEMPT_LIMIT", "many"),
            ("COOKIE_SECURE", "maybe"), ("APP_ENV", "staging"),
            ("PHOTO_BACKEND", "public"), ("EMAIL_BACKEND", "smtp"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ConfigurationError):
                    RuntimeSettings.from_env(environment(**{name: value}))


class LocalStorageTests(unittest.TestCase):
    def setUp(self):
        try:
            import PIL
        except ImportError:
            self.skipTest("Pillow is required for image validation tests")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve() / "private" / "photos"
        self.storage = LocalFileStorage(self.root)

    def test_construction_does_not_create_files(self):
        self.assertFalse(self.root.exists())

    def test_photo_is_private_and_stripped_of_metadata(self):
        from PIL import Image
        stored = self.storage.put(png_photo(), "image/png")
        data, content_type = self.storage.read(stored.key)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(stored.size, len(data))
        self.assertRegex(stored.key, r"^[0-9a-f]{64}\.png$")
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / stored.key).stat().st_mode), 0o600)
        with Image.open(io.BytesIO(data)) as image:
            self.assertNotIn("Employee", image.info)
        self.storage.delete(stored.key)
        with self.assertRaisesRegex(DomainError, "PHOTO_NOT_FOUND"):
            self.storage.read(stored.key)

    def test_each_upload_has_an_independent_random_key(self):
        first = self.storage.put(png_photo(), "image/png")
        second = self.storage.put(png_photo(), "image/png")
        self.assertNotEqual(first.key, second.key)

    def test_rejects_mislabeled_content(self):
        with self.assertRaisesRegex(DomainError, "PHOTO_CONTENT_TYPE_MISMATCH"):
            self.storage.put(png_photo(), "image/jpeg")

    def test_rejects_svg(self):
        with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_TYPE"):
            self.storage.put(b'<svg onload="alert(1)"></svg>', "image/svg+xml")

    def test_rejects_invalid_photo_and_size(self):
        for data in (b"", b"not an image", b"0" * (self.storage.max_bytes + 1)):
            with self.subTest(size=len(data)):
                with self.assertRaises(DomainError):
                    self.storage.put(data, "image/png")

    def test_rejects_path_traversal(self):
        for key in ("../secret", "/etc/passwd", "photos/example.png", "a" * 64 + ".svg"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_KEY"):
                    self.storage.read(key)
                with self.assertRaisesRegex(DomainError, "INVALID_PHOTO_KEY"):
                    self.storage.delete(key)

    def test_rejects_symlink_file(self):
        stored = self.storage.put(png_photo(), "image/png")
        original = self.root / stored.key
        target = self.root.parent / "secret"
        target.write_bytes(b"private")
        original.unlink()
        original.symlink_to(target)
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.read(stored.key)
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.delete(stored.key)
        self.assertEqual(target.read_bytes(), b"private")

    def test_rejects_symlink_root(self):
        target = self.root.parent
        target.mkdir(parents=True)
        self.root.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.put(png_photo(), "image/png")

    def test_rejects_symlink_ancestor_before_creating_directories(self):
        target = Path(self.directory.name).resolve() / "outside"
        target.mkdir()
        self.root.parent.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.put(png_photo(), "image/png")
        self.assertEqual(list(target.iterdir()), [])

    def test_rejects_fifo_without_waiting_for_a_writer(self):
        stored = self.storage.put(png_photo(), "image/png")
        path = self.root / stored.key
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.read(stored.key)

    def test_rejects_hardlink_file(self):
        stored = self.storage.put(png_photo(), "image/png")
        os.link(self.root / stored.key, self.root.parent / "second-link")
        with self.assertRaisesRegex(DomainError, "UNSAFE_PHOTO_STORAGE"):
            self.storage.read(stored.key)

    def test_s3_upload_is_private_and_encrypted_with_fake_client(self):
        client = Mock()
        storage = S3Storage("example-bucket", "ap-south-1", client=client)
        stored = storage.put(png_photo(), "image/png")
        arguments = client.put_object.call_args.kwargs
        self.assertEqual(arguments["Key"], stored.key)
        self.assertEqual(arguments["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", arguments)


class EmailDeliveryTests(unittest.TestCase):
    def preview(self):
        return EmailPreview(
            email_id=1,
            recipient_email="recipient@example.com",
            employee_name='<script>alert("unsafe")</script>',
            token="sensitive-token",
            qr_svg='<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        )

    def test_local_preview_escapes_name_and_does_not_expose_raw_token(self):
        rendered = LocalEmailPreview().render(self.preview())
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("sensitive-token", rendered)
        self.assertIn("data:image/svg+xml;base64,", rendered)

    def test_ses_requires_feature_flag_and_explicit_send_approval(self):
        for enabled, approved in ((False, False), (False, True), (True, False)):
            with self.subTest(enabled=enabled, approved=approved):
                client = Mock()
                adapter = SESDelivery("sender@example.com", "ap-south-1", enabled, client)
                with self.assertRaisesRegex(DomainError, "REAL_EMAIL_NOT_AUTHORIZED"):
                    adapter.send(self.preview(), allow_real_email=approved)
                client.send_raw_email.assert_not_called()

    def test_ses_formats_message_using_fake_client_only(self):
        from dataclasses import replace
        from meal_management.security import generate_token

        client = Mock()
        client.send_raw_email.return_value = {"MessageId": "fictional-message-id"}
        adapter = SESDelivery("sender@example.com", "ap-south-1", True, client)
        preview = replace(self.preview(), recipient_email="recipient@gmail.com", token=generate_token(), qr_svg="")
        result = adapter.send(preview, allow_real_email=True)
        self.assertEqual(result, "fictional-message-id")
        self.assertEqual(client.send_raw_email.call_args.kwargs["Destinations"], ["recipient@gmail.com"])


if __name__ == "__main__":
    unittest.main()
