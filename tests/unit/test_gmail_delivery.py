import io
import smtplib
import ssl
import unittest
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock, patch

from PIL import Image

from meal_management.delivery import DeliveryError, GmailDelivery, SESDelivery, delivery_from_settings, validate_email_recipient
from meal_management.email_queue import EmailPreview
from meal_management.errors import ConfigurationError
from meal_management.runtime import RuntimeSettings
from meal_management.security import generate_token


def gmail_environment(**values):
    return {
        "APP_CSRF_SECRET": "c" * 32, "LOGIN_RATE_SECRET": "r" * 32,
        "EMAIL_BACKEND": "gmail", "EMAIL_SENDER": "Meal.Office@gmail.com",
        "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop", **values,
    }


class GmailConfigurationTests(unittest.TestCase):
    def test_gmail_works_with_local_storage_without_aws_and_stays_disabled(self):
        settings = RuntimeSettings.from_env(gmail_environment())
        self.assertEqual(settings.email_backend, "gmail")
        self.assertEqual(settings.photo_backend, "local")
        self.assertEqual(settings.email_sender, "meal.office@gmail.com")
        self.assertEqual(settings.gmail_app_password, "abcdefghijklmnop")
        self.assertIsNone(settings.aws_region)
        self.assertIsNone(settings.photo_bucket)
        self.assertFalse(settings.email_send_enabled)
        with patch("meal_management.delivery.smtplib.SMTP_SSL") as smtp:
            adapter = delivery_from_settings(settings)
        self.assertIsInstance(adapter, GmailDelivery)
        self.assertFalse(adapter.enabled)
        smtp.assert_not_called()

    def test_gmail_password_is_hidden_in_runtime_and_adapter_representations(self):
        settings = RuntimeSettings.from_env(gmail_environment())
        self.assertNotIn("abcdefghijklmnop", repr(settings))
        self.assertNotIn("abcdefghijklmnop", repr(delivery_from_settings(settings)))

    def test_gmail_requires_sender_and_app_password_even_when_disabled(self):
        for setting in ("EMAIL_SENDER", "GMAIL_APP_PASSWORD"):
            with self.subTest(setting=setting):
                values = gmail_environment()
                values.pop(setting)
                with self.assertRaises(ConfigurationError) as caught:
                    RuntimeSettings.from_env(values)
                self.assertEqual(caught.exception.code, "MISSING_SETTING_" + setting)

    def test_invalid_app_passwords_are_rejected_without_echoing_values(self):
        for value in ("short-private", "1234567890123456", "abcd\tefghijklmnop", "abcdefghijklmnopq", "abcd-efgh-ijkl-mnop"):
            with self.subTest(length=len(value)):
                with self.assertRaises(ConfigurationError) as caught:
                    RuntimeSettings.from_env(gmail_environment(GMAIL_APP_PASSWORD=value))
                self.assertEqual(str(caught.exception), "INVALID_SETTING_GMAIL_APP_PASSWORD")
                self.assertNotIn(value, str(caught.exception))

    def test_workspace_username_is_supported_and_header_injection_is_rejected(self):
        settings = RuntimeSettings.from_env(gmail_environment(EMAIL_SENDER="meals@workplace.co.uk"))
        self.assertEqual(settings.email_sender, "meals@workplace.co.uk")
        for value in ("Alias <sender@gmail.com>", "sender@gmail.com\r\nBcc: other@gmail.com", "a..b@gmail.com"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ConfigurationError, "INVALID_SETTING_EMAIL_SENDER"):
                    RuntimeSettings.from_env(gmail_environment(EMAIL_SENDER=value))


class GmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.smtp = Mock()
        self.smtp.mail.return_value = (250, b"OK")
        self.smtp.rcpt.return_value = (250, b"OK")
        self.smtp.docmd.return_value = (354, b"Send data")
        self.smtp.getreply.return_value = (250, b"accepted fictional provider response")
        self.factory = Mock(return_value=self.smtp)
        self.adapter = GmailDelivery("meal.office@gmail.com", "abcd efgh ijkl mnop", True, self.factory)
        self.preview = EmailPreview(7, "recipient@gmail.com", "Fictional Employee", generate_token(), "<svg>untrusted archived content</svg>")

    def test_sending_requires_feature_flag_and_explicit_boolean_approval(self):
        for enabled, approved in ((False, False), (False, True), (True, False), (True, "true"), (True, 1)):
            with self.subTest(enabled=enabled, approved=approved):
                adapter = GmailDelivery("meal.office@gmail.com", "abcdefghijklmnop", enabled, self.factory)
                with self.assertRaises(DeliveryError) as caught:
                    adapter.send(self.preview, allow_real_email=approved)
                self.assertEqual(caught.exception.code, "REAL_EMAIL_NOT_AUTHORIZED")
                self.assertFalse(caught.exception.uncertain)
        self.factory.assert_not_called()

    def test_verified_tls_fixed_gmail_server_and_bounded_timeout_are_used(self):
        identifier = self.adapter.send(self.preview, allow_real_email=True)
        self.assertEqual(self.factory.call_args.args, ("smtp.gmail.com", 465))
        options = self.factory.call_args.kwargs
        self.assertEqual(options["timeout"], 20)
        self.assertEqual(options["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(options["context"].check_hostname)
        self.smtp.set_debuglevel.assert_called_once_with(0)
        self.smtp.login.assert_called_once_with("meal.office@gmail.com", "abcdefghijklmnop")
        self.smtp.mail.assert_called_once_with("meal.office@gmail.com")
        self.smtp.rcpt.assert_called_once_with("recipient@gmail.com")
        self.smtp.docmd.assert_called_once_with("DATA")
        self.smtp.close.assert_called_once()
        self.assertRegex(identifier, r"^<[0-9a-f]{32}@gmail\.com>$")
        self.assertNotIn("provider", identifier)

    def test_message_attaches_png_generated_from_token_and_ignores_archived_svg(self):
        import qrcode

        with patch("qrcode.make", wraps=qrcode.make) as make:
            identifier = self.adapter.send(self.preview, allow_real_email=True)
        make.assert_called_once_with(self.preview.token)
        wire = self.smtp.send.call_args.args[0]
        self.assertTrue(wire.endswith(b"\r\n.\r\n"))
        self.assertNotIn(self.preview.token.encode(), wire)
        self.assertNotIn(b"untrusted archived content", wire)
        message = BytesParser(policy=policy.default).parsebytes(wire[:-3])
        self.assertEqual(str(message["Message-ID"]), identifier)
        self.assertEqual(str(message["To"]), "recipient@gmail.com")
        self.assertEqual(message.get_body().get_content_type(), "text/plain")
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_filename(), "meal-qr.png")
        self.assertEqual(attachments[0].get_content_type(), "image/png")
        with Image.open(io.BytesIO(attachments[0].get_payload(decode=True))) as picture:
            self.assertEqual(picture.format, "PNG")
            self.assertGreater(picture.width, 100)

    def test_fictional_domains_and_malformed_addresses_never_connect(self):
        for recipient in (
            "visitor@example.test", "visitor@company.test", "visitor@example.com", "visitor@mail.example.org",
            "visitor@example.net", "visitor@company.invalid", "visitor@site.localhost",
            "visitor@gmail.com\r\nBcc: other@gmail.com", "Name <visitor@gmail.com>", "no-at-symbol", "a..b@gmail.com",
        ):
            with self.subTest(recipient=recipient):
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(replace(self.preview, recipient_email=recipient), allow_real_email=True)
                self.assertFalse(caught.exception.uncertain)
                self.assertNotIn(recipient, str(caught.exception))
        self.factory.assert_not_called()

    def test_shared_recipient_validation_normalizes_and_rejects_test_addresses(self):
        self.assertEqual(validate_email_recipient(" Recipient+Meals@GMAIL.COM "), "recipient+meals@gmail.com")
        with self.assertRaisesRegex(DeliveryError, "TEST_EMAIL_RECIPIENT_FORBIDDEN"):
            validate_email_recipient("visitor@example.test")

    def test_invalid_qr_and_message_content_fail_before_connecting(self):
        for preview, code in (
            (replace(self.preview, token="not-a-credential"), "INVALID_QR"),
            (replace(self.preview, employee_name="Name\r\nInjection"), "INVALID_EMAIL_CONTENT"),
            (None, "INVALID_EMAIL_CONTENT"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(preview, allow_real_email=True)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.uncertain)
        self.factory.assert_not_called()

    def test_definite_smtp_rejections_are_safe_and_not_uncertain(self):
        for method, response, code in (
            ("mail", (550, b"fictional sensitive sender response"), "EMAIL_SENDER_REJECTED"),
            ("rcpt", (550, b"fictional sensitive recipient response"), "EMAIL_RECIPIENT_REJECTED"),
            ("docmd", (554, b"fictional sensitive DATA rejection"), "EMAIL_DATA_REJECTED"),
            ("getreply", (550, b"fictional sensitive final rejection"), "EMAIL_DATA_REJECTED"),
        ):
            with self.subTest(method=method):
                original = getattr(self.smtp, method).return_value
                getattr(self.smtp, method).return_value = response
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(self.preview, allow_real_email=True)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.uncertain)
                self.assertNotIn("sensitive", str(caught.exception))
                getattr(self.smtp, method).return_value = original

    def test_authentication_failure_is_definite_and_secret_safe(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"fictional-password-and-provider-details")
        with self.assertRaises(DeliveryError) as caught:
            self.adapter.send(self.preview, allow_real_email=True)
        self.assertEqual(caught.exception.code, "EMAIL_AUTHENTICATION_FAILED")
        self.assertFalse(caught.exception.uncertain)
        self.assertNotIn("fictional-password", str(caught.exception))
        self.smtp.mail.assert_not_called()
        self.smtp.send.assert_not_called()
        self.factory.assert_called_once()

    def test_connection_failures_before_data_are_definite_without_retries(self):
        for stage in ("connection", "ehlo_or_helo_if_needed", "docmd"):
            with self.subTest(stage=stage):
                self.factory.reset_mock()
                target = self.factory if stage == "connection" else getattr(self.smtp, stage)
                target.side_effect = smtplib.SMTPServerDisconnected("fictional-private-provider-details")
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(self.preview, allow_real_email=True)
                self.assertEqual(caught.exception.code, "EMAIL_DELIVERY_FAILED")
                self.assertFalse(caught.exception.uncertain)
                self.assertNotIn("private-provider", str(caught.exception))
                self.factory.assert_called_once()
                target.side_effect = None

    def test_connection_failure_during_body_or_final_reply_is_uncertain(self):
        for method in ("send", "getreply"):
            with self.subTest(method=method):
                self.factory.reset_mock()
                getattr(self.smtp, method).side_effect = OSError("fictional-password-and-server-details")
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(self.preview, allow_real_email=True)
                self.assertEqual(caught.exception.code, "EMAIL_DELIVERY_UNCONFIRMED")
                self.assertTrue(caught.exception.uncertain)
                self.assertNotIn("fictional-password", str(caught.exception))
                self.factory.assert_called_once()
                getattr(self.smtp, method).side_effect = None

    def test_cleanup_failure_does_not_replace_accepted_result(self):
        self.smtp.close.side_effect = OSError("fictional-sensitive-close-failure")
        identifier = self.adapter.send(self.preview, allow_real_email=True)
        self.assertRegex(identifier, r"^<[0-9a-f]{32}@gmail\.com>$")
        self.smtp.send.assert_called_once()

    def test_unexpected_final_reply_is_uncertain_after_data_was_sent(self):
        for code in (354, 200, -1, "250", True):
            with self.subTest(code=code):
                self.smtp.getreply.return_value = (code, b"fictional unexpected response")
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.send(self.preview, allow_real_email=True)
                self.assertEqual(caught.exception.code, "EMAIL_DELIVERY_UNCONFIRMED")
                self.assertTrue(caught.exception.uncertain)

    def test_ses_generates_png_from_token_when_worker_omits_svg(self):
        client = Mock()
        client.send_raw_email.return_value = {"MessageId": "fictional-message-id"}
        adapter = SESDelivery("meals@workplace.co.uk", "ap-south-1", True, client)
        self.assertEqual(adapter.send(replace(self.preview, qr_svg=""), allow_real_email=True), "fictional-message-id")
        wire = client.send_raw_email.call_args.kwargs["RawMessage"]["Data"]
        message = BytesParser(policy=policy.default).parsebytes(wire)
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].get_content_type(), "image/png")
        self.assertEqual(attachments[0].get_filename(), "meal-qr.png")
        with Image.open(io.BytesIO(attachments[0].get_payload(decode=True))) as picture:
            self.assertEqual(picture.format, "PNG")

    def test_ses_rejects_test_recipient_before_client_access(self):
        client = Mock()
        adapter = SESDelivery("meals@workplace.co.uk", "ap-south-1", True, client)
        with self.assertRaisesRegex(DeliveryError, "TEST_EMAIL_RECIPIENT_FORBIDDEN"):
            adapter.send(replace(self.preview, recipient_email="visitor@example.test"), allow_real_email=True)
        client.send_raw_email.assert_not_called()

    def test_timeout_and_password_configuration_are_bounded(self):
        for timeout in (0, 31, True, "20"):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(DeliveryError, "GMAIL_CONFIGURATION_REQUIRED"):
                    GmailDelivery("meal.office@gmail.com", "abcdefghijklmnop", smtp_factory=self.factory, timeout=timeout)
        with self.assertRaisesRegex(DeliveryError, "GMAIL_CONFIGURATION_REQUIRED"):
            GmailDelivery("meal.office@gmail.com", "ordinary-password", smtp_factory=self.factory)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
