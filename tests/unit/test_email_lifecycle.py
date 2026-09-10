import asyncio
import unittest
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from meal_management.email_lifecycle import EmailLifecycle, _build_dispatcher
from meal_management.errors import ConfigurationError
from meal_management.runtime import RuntimeSettings

from test_api import ApiHarness


def environment(**changes):
    values = {
        "APP_CSRF_SECRET": "a" * 32,
        "LOGIN_RATE_SECRET": "b" * 32,
        "APP_ORIGIN": "http://testserver",
        "SCANNER_ORIGIN": "http://testserver:8001",
        "ALLOWED_HOSTS": "testserver",
    }
    values.update(changes)
    return values


def enabled_runtime(**changes):
    return RuntimeSettings.from_env(environment(
        EMAIL_BACKEND="gmail", EMAIL_SEND_ENABLED="true", EMAIL_AUTO_SEND_ENABLED="true",
        EMAIL_SENDER="fictional.sender@gmail.com", GMAIL_APP_PASSWORD="abcdefghijklmnop", **changes,
    ))


class ControlledDispatcher:
    def __init__(self, hold_after_stop=False):
        self.entered = Event()
        self.stop_observed = Event()
        self.release = Event()
        self.finished = Event()
        self.hold_after_stop = hold_after_stop
        self.calls = 0

    def run(self, stop_event):
        self.calls += 1
        self.entered.set()
        stop_event.wait(5)
        self.stop_observed.set()
        if self.hold_after_stop:
            self.release.wait(5)
        self.finished.set()


class AutomaticEmailConfigurationTests(unittest.TestCase):
    def test_defaults_disable_automatic_and_real_delivery(self):
        settings = RuntimeSettings.from_env(environment())
        self.assertFalse(settings.email_send_enabled)
        self.assertFalse(settings.email_auto_send_enabled)
        self.assertEqual(settings.email_poll_seconds, 5)
        self.assertEqual(settings.email_batch_size, 10)

    def test_gmail_automatic_delivery_requires_no_aws_settings(self):
        settings = enabled_runtime(EMAIL_POLL_SECONDS="7", EMAIL_BATCH_SIZE="12")
        self.assertTrue(settings.email_auto_send_enabled)
        self.assertTrue(settings.email_send_enabled)
        self.assertEqual(settings.email_poll_seconds, 7)
        self.assertEqual(settings.email_batch_size, 12)
        self.assertIsNone(settings.aws_region)

    def test_automatic_delivery_requires_both_flags_and_real_backend(self):
        for backend, send in (("preview", "false"), ("preview", "true"), ("gmail", "false"), ("ses", "false")):
            with self.subTest(backend=backend, send=send):
                with self.assertRaisesRegex(ConfigurationError, "^EMAIL_AUTO_SEND_REQUIRES_ENABLED_REAL_EMAIL$"):
                    RuntimeSettings.from_env(environment(
                        EMAIL_BACKEND=backend, EMAIL_SEND_ENABLED=send, EMAIL_AUTO_SEND_ENABLED="true",
                    ))

    def test_automatic_flag_rejects_nonboolean_values(self):
        with self.assertRaisesRegex(ConfigurationError, "^INVALID_SETTING_EMAIL_AUTO_SEND_ENABLED$"):
            RuntimeSettings.from_env(environment(EMAIL_AUTO_SEND_ENABLED="sometimes"))

    def test_poll_and_batch_bounds_are_inclusive(self):
        for poll, batch in ((1, 1), (300, 100)):
            with self.subTest(poll=poll, batch=batch):
                settings = enabled_runtime(EMAIL_POLL_SECONDS=str(poll), EMAIL_BATCH_SIZE=str(batch))
                self.assertEqual(settings.email_poll_seconds, poll)
                self.assertEqual(settings.email_batch_size, batch)

    def test_invalid_poll_and_batch_values_are_rejected_even_when_disabled(self):
        for name, values in (
            ("EMAIL_POLL_SECONDS", ("0", "301", "1.5", "secret-value")),
            ("EMAIL_BATCH_SIZE", ("0", "101", "1.5", "secret-value")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ConfigurationError) as caught:
                        RuntimeSettings.from_env(environment(**{name: value}))
                    self.assertEqual(str(caught.exception), "INVALID_SETTING_" + name)

    def test_production_does_not_turn_automatic_delivery_on(self):
        settings = RuntimeSettings.from_env(environment(
            APP_ENV="production", APP_ORIGIN="https://admin.example.test", ALLOWED_HOSTS="admin.example.test",
            PHOTO_BACKEND="s3", PHOTO_S3_BUCKET="fictional-private-photos", AWS_REGION="ap-south-1",
            EMAIL_BACKEND="ses", EMAIL_SENDER="sender@company.example.test", EMAIL_SEND_ENABLED="true",
        ))
        self.assertFalse(settings.email_auto_send_enabled)


class EmailLifecycleConstructionTests(unittest.TestCase):
    def test_construction_is_inert_and_does_not_create_dispatcher(self):
        services = Mock()
        with patch("meal_management.email_lifecycle._build_dispatcher") as build:
            lifecycle = EmailLifecycle(enabled_runtime(), services)
            self.assertFalse(lifecycle.snapshot()["worker_running"])
        build.assert_not_called()
        services.assert_not_called()
        self.assertEqual(services.mock_calls, [])

    def test_disabled_or_manual_delivery_never_constructs_sender_or_dispatcher(self):
        for runtime in (
            RuntimeSettings.from_env(environment()),
            replace(enabled_runtime(), email_send_enabled=False, email_auto_send_enabled=False),
            replace(enabled_runtime(), email_auto_send_enabled=False),
        ):
            with self.subTest(backend=runtime.email_backend, send=runtime.email_send_enabled):
                services = Mock()
                with patch("meal_management.email_lifecycle._build_dispatcher") as build:
                    EmailLifecycle(runtime, services).start()
                build.assert_not_called()
                self.assertEqual(services.mock_calls, [])

    def test_injected_settings_cannot_bypass_auto_delivery_guard(self):
        for runtime in (
            replace(enabled_runtime(), email_send_enabled=False),
            replace(enabled_runtime(), email_backend="preview"),
        ):
            with self.subTest(backend=runtime.email_backend):
                with self.assertRaisesRegex(ConfigurationError, "^EMAIL_AUTO_SEND_REQUIRES_ENABLED_REAL_EMAIL$"):
                    EmailLifecycle(runtime, Mock())

    def test_builder_reuses_existing_database_vault_and_renderer(self):
        runtime = enabled_runtime(EMAIL_POLL_SECONDS="11", EMAIL_BATCH_SIZE="4")
        services = SimpleNamespace(database=Mock(), qr=SimpleNamespace(vault=Mock(), renderer=Mock()))
        with (
            patch("meal_management.email_lifecycle.delivery_from_settings") as delivery,
            patch("meal_management.email_lifecycle.EmailDeliveryWorker") as worker,
            patch("meal_management.email_dispatcher.EmailDispatcher") as dispatcher,
        ):
            result = _build_dispatcher(runtime, services)
        delivery.assert_called_once_with(runtime)
        worker.assert_called_once_with(services.database, services.qr.vault, services.qr.renderer, delivery.return_value)
        dispatcher.assert_called_once_with(services.database, worker.return_value, interval_seconds=11, batch_size=4)
        self.assertIs(result, dispatcher.return_value)
        self.assertEqual(services.database.mock_calls, [])


class EmailLifecycleThreadTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_shutdown_is_inert(self):
        lifecycle = EmailLifecycle(RuntimeSettings.from_env(environment()), Mock())
        with patch("meal_management.email_lifecycle.run_in_threadpool") as join:
            await lifecycle.stop()
        join.assert_not_called()
        self.assertFalse(lifecycle.snapshot()["worker_running"])

    async def test_start_is_nonblocking_and_repeated_start_keeps_one_worker(self):
        dispatcher = ControlledDispatcher()
        lifecycle = EmailLifecycle(enabled_runtime(), Mock())
        try:
            with patch("meal_management.email_lifecycle._build_dispatcher", return_value=dispatcher) as build:
                lifecycle.start()
                self.assertTrue(await asyncio.to_thread(dispatcher.entered.wait, 1))
                lifecycle.start()
                self.assertTrue(lifecycle.snapshot()["worker_running"])
                self.assertFalse(dispatcher.finished.is_set())
                build.assert_called_once()
                self.assertEqual(dispatcher.calls, 1)
        finally:
            await lifecycle.stop()
        self.assertTrue(dispatcher.stop_observed.is_set())
        self.assertTrue(dispatcher.finished.is_set())
        self.assertFalse(lifecycle.snapshot()["worker_running"])

    async def test_shutdown_waits_for_active_attempt_without_blocking_event_loop(self):
        dispatcher = ControlledDispatcher(hold_after_stop=True)
        lifecycle = EmailLifecycle(enabled_runtime(), Mock())
        try:
            with patch("meal_management.email_lifecycle._build_dispatcher", return_value=dispatcher):
                lifecycle.start()
            self.assertTrue(await asyncio.to_thread(dispatcher.entered.wait, 1))
            stopping = asyncio.create_task(lifecycle.stop())
            self.assertTrue(await asyncio.to_thread(dispatcher.stop_observed.wait, 1))
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            self.assertTrue(lifecycle.snapshot()["worker_running"])
            dispatcher.release.set()
            await asyncio.wait_for(stopping, 1)
        finally:
            dispatcher.release.set()
            await lifecycle.stop()
        self.assertTrue(dispatcher.finished.is_set())
        self.assertFalse(lifecycle.snapshot()["worker_running"])

    async def test_exceptional_hang_has_bounded_shutdown_and_safe_status(self):
        dispatcher = ControlledDispatcher(hold_after_stop=True)
        lifecycle = EmailLifecycle(enabled_runtime(), Mock(), shutdown_timeout=0.01)
        try:
            with patch("meal_management.email_lifecycle._build_dispatcher", return_value=dispatcher):
                lifecycle.start()
            self.assertTrue(await asyncio.to_thread(dispatcher.entered.wait, 1))
            await asyncio.wait_for(lifecycle.stop(), 1)
            self.assertTrue(dispatcher.stop_observed.is_set())
            self.assertEqual(lifecycle.error_code, "EMAIL_SHUTDOWN_TIMEOUT")
            self.assertTrue(lifecycle.snapshot()["worker_running"])
            self.assertTrue(lifecycle._thread.daemon)
            self.assertNotIn("error_code", lifecycle.snapshot())
        finally:
            dispatcher.release.set()
            await asyncio.to_thread(dispatcher.finished.wait, 1)
            await lifecycle.stop()
        self.assertFalse(lifecycle.snapshot()["worker_running"])

    async def test_unexpected_dispatcher_exception_is_not_logged_or_exposed(self):
        dispatcher = Mock()
        dispatcher.run.side_effect = RuntimeError("fictional-password-and-recipient-secret")
        lifecycle = EmailLifecycle(enabled_runtime(), Mock())
        with (
            patch("meal_management.email_lifecycle._build_dispatcher", return_value=dispatcher),
            patch("threading.excepthook") as unhandled,
        ):
            lifecycle.start()
            await lifecycle.stop()
        unhandled.assert_not_called()
        self.assertEqual(lifecycle.error_code, "EMAIL_DISPATCHER_STOPPED")
        self.assertNotIn("fictional-password", str(lifecycle.snapshot()))
        self.assertFalse(lifecycle.snapshot()["worker_running"])


class EmailLifecycleApiTests(unittest.TestCase):
    def test_disabled_admin_startup_never_polls_or_builds_delivery(self):
        with patch("meal_management.email_lifecycle._build_dispatcher") as build:
            harness = ApiHarness(RuntimeSettings.from_env(environment()))
            with harness.client:
                self.assertEqual(harness.client.get("/health/live").status_code, 200)
            build.assert_not_called()
            harness.services.database.transaction.assert_not_called()

    def test_manual_provider_startup_never_builds_delivery(self):
        with patch("meal_management.email_lifecycle._build_dispatcher") as build:
            harness = ApiHarness(replace(enabled_runtime(), email_auto_send_enabled=False))
            with harness.client:
                self.assertEqual(harness.client.get("/health/live").status_code, 200)
            build.assert_not_called()
            harness.services.database.transaction.assert_not_called()

    def test_enabled_admin_starts_and_drains_background_worker_with_safe_metadata(self):
        dispatcher = ControlledDispatcher()
        with patch("meal_management.email_lifecycle._build_dispatcher", return_value=dispatcher) as build:
            harness = ApiHarness(enabled_runtime(EMAIL_POLL_SECONDS="8", EMAIL_BATCH_SIZE="3"))
            build.assert_not_called()
            with harness.client:
                self.assertTrue(dispatcher.entered.wait(1))
                self.assertEqual(harness.client.get("/health/live").status_code, 200)
                self.assertEqual(harness.client.get("/api/email-settings").status_code, 401)
                harness.login("waiter")
                self.assertEqual(harness.client.get("/api/email-settings").status_code, 403)
                harness.login()
                response = harness.client.get("/api/email-settings")
                self.assertEqual(response.json(), {
                    "backend": "gmail", "sending_enabled": True, "preview_available": False,
                    "approval_required": True,
                    "automatic_enabled": True, "worker_running": True, "poll_seconds": 8, "batch_size": 3,
                })
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertNotIn("fictional.sender", response.text)
                self.assertNotIn("abcdefghijklmnop", response.text)
                self.assertFalse(dispatcher.finished.is_set())
            build.assert_called_once()
        self.assertTrue(dispatcher.stop_observed.is_set())
        self.assertTrue(dispatcher.finished.is_set())
        self.assertFalse(harness.app.state.email_lifecycle.snapshot()["worker_running"])
        harness.services.database.transaction.assert_not_called()

    def test_scanner_never_constructs_or_starts_email_lifecycle_when_flags_enabled(self):
        with (
            patch("meal_management.api.EmailLifecycle") as lifecycle,
            patch("meal_management.email_lifecycle._build_dispatcher") as build,
            patch("meal_management.email_lifecycle.delivery_from_settings") as delivery,
        ):
            harness = ApiHarness(enabled_runtime(), application="scanner")
            with harness.client:
                self.assertEqual(harness.client.get("/health/live").status_code, 200)
                self.assertEqual(harness.client.get("/api/email-settings").status_code, 404)
            lifecycle.assert_not_called()
            build.assert_not_called()
            delivery.assert_not_called()
        self.assertFalse(hasattr(harness.app.state, "email_lifecycle"))
        harness.services.database.transaction.assert_not_called()
