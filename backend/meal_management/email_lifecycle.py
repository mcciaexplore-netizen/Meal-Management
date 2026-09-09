from threading import Event, Lock, Thread

from starlette.concurrency import run_in_threadpool

from .delivery import delivery_from_settings
from .email_worker import EmailDeliveryWorker
from .errors import ConfigurationError


def _build_dispatcher(runtime, services):
    from .email_dispatcher import EmailDispatcher

    delivery = delivery_from_settings(runtime)
    worker = EmailDeliveryWorker(services.database, services.qr.vault, services.qr.renderer, delivery)
    return EmailDispatcher(
        services.database, worker,
        interval_seconds=runtime.email_poll_seconds, batch_size=runtime.email_batch_size,
    )


class EmailLifecycle:
    def __init__(self, runtime, services, shutdown_timeout=300):
        if runtime.email_auto_send_enabled is True and (
            runtime.email_send_enabled is not True or runtime.email_backend not in {"gmail", "ses"}
        ):
            raise ConfigurationError("EMAIL_AUTO_SEND_REQUIRES_ENABLED_REAL_EMAIL")
        self.runtime = runtime
        self.services = services
        self.shutdown_timeout = shutdown_timeout
        self._lock = Lock()
        self._stop_event = None
        self._thread = None
        self._error_code = None

    def start(self):
        if self.runtime.email_auto_send_enabled is not True or self.runtime.email_send_enabled is not True:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            dispatcher = _build_dispatcher(self.runtime, self.services)
            self._stop_event = Event()
            self._error_code = None
            self._thread = Thread(
                target=self._run, args=(dispatcher, self._stop_event),
                name="meal-email-dispatcher", daemon=True,
            )
            self._thread.start()

    def _run(self, dispatcher, stop_event):
        try:
            dispatcher.run(stop_event)
        except Exception:
            with self._lock:
                self._error_code = "EMAIL_DISPATCHER_STOPPED"

    async def stop(self):
        with self._lock:
            thread = self._thread
            if self._stop_event is not None:
                self._stop_event.set()
        if thread is None:
            return
        await run_in_threadpool(thread.join, self.shutdown_timeout)
        if thread.is_alive():
            with self._lock:
                self._error_code = "EMAIL_SHUTDOWN_TIMEOUT"

    def snapshot(self):
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
        return {
            "automatic_enabled": self.runtime.email_auto_send_enabled is True,
            "worker_running": running,
            "poll_seconds": self.runtime.email_poll_seconds,
            "batch_size": self.runtime.email_batch_size,
        }

    @property
    def error_code(self):
        with self._lock:
            return self._error_code
