from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api_common import database_readiness, scan_response
from .api_schemas import ScannerActivationBody, ScannerReadBody, ScannerVisitorBody
from .application import create_services
from .errors import ConfigurationError, DomainError
from .http_security import SecurityMiddleware, error_body, status_for
from .runtime import RuntimeSettings
from .scan_app import ScanAppService
from .scan_receipts import ScanReceiptService
from .scanner_access import ScannerBrowser, ScannerLimiter


def create_app(services=None, runtime=None, scanner=None, limiter=None):
    runtime = (RuntimeSettings.from_env() if runtime is None else runtime).for_application("scanner")
    services = create_services() if services is None else services
    if runtime.environment == "production" and not services.database.settings.db_ssl_ca:
        raise ConfigurationError("PRODUCTION_DATABASE_TLS_REQUIRED")
    app = FastAPI(title="Meal scanner", version="0.3.0", docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.state.services = services
    app.state.runtime = runtime
    app.state.scanner = ScanAppService(services.database, services.meals, ScanReceiptService(services.database)) if scanner is None else scanner
    app.state.scanner_limiter = ScannerLimiter(services.database, runtime) if limiter is None else limiter
    scanner_browser = ScannerBrowser(runtime.csrf_secret, runtime.scanner_activation_secret)
    scanner_cookie = "__Host-meal_scanner" if runtime.cookie_secure else "meal_scanner"
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(runtime.allowed_hosts))
    app.add_middleware(SecurityMiddleware, settings=runtime)

    def enabled():
        if not runtime.scan_app_enabled:
            raise DomainError("SCANNER_DISABLED")

    def browser_context(request: Request):
        enabled()
        cookie = request.cookies.get(scanner_cookie)
        identity = scanner_browser.identity(cookie)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            scanner_browser.verify_csrf(cookie, request.headers.get("x-csrf-token", ""))
            if not getattr(request.state, "scanner_rate_checked", False):
                address = request.client.host if request.client else "unknown"
                app.state.scanner_limiter.consume(identity, address)
                request.state.scanner_rate_checked = True
        return identity

    def same_origin(request):
        origin = request.headers.get("origin")
        if (origin is not None and origin != runtime.app_origin) or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
            raise DomainError("ORIGIN_REJECTED")

    def session_payload(cookie):
        return {"scope": scanner_browser.identity(cookie).hex(), "csrf_token": scanner_browser.csrf(cookie)}

    def set_scanner_cookie(response, cookie):
        response.set_cookie(scanner_cookie, cookie, httponly=True, secure=runtime.cookie_secure,
                            samesite="strict", path="/", max_age=30 * 24 * 3600)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        headers = {"Retry-After": str(runtime.scanner_window_seconds)} if error.code == "SCANNER_RATE_LIMITED" else None
        return JSONResponse(error_body(error.code, getattr(request.state, "request_id", None)), status_code=status_for(error.code), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path in {"/api/scanner/read", "/api/scanner/visitors"}:
            try:
                browser_hash = await run_in_threadpool(browser_context, request)
                await run_in_threadpool(app.state.scanner.record_invalid, browser_hash)
            except DomainError as failure:
                return await domain_error(request, failure)
            except Exception:
                return JSONResponse(error_body("SERVICE_UNAVAILABLE", request.state.request_id), status_code=503)
        return JSONResponse(error_body("INVALID_INPUT", getattr(request.state, "request_id", None)), status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        code = "NOT_FOUND" if error.status_code == 404 else "INVALID_INPUT"
        return JSONResponse(error_body(code, getattr(request.state, "request_id", None)), status_code=error.status_code)

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        enabled()
        return database_readiness(services.database)

    @app.get("/api/scanner/session")
    def session(request: Request, response: Response):
        enabled()
        same_origin(request)
        cookie = request.cookies.get(scanner_cookie)
        if not scanner_browser.valid(cookie):
            if runtime.environment == "production":
                raise DomainError("SCANNER_ACTIVATION_REQUIRED")
            cookie = scanner_browser.issue()
            response = JSONResponse(session_payload(cookie))
            set_scanner_cookie(response, cookie)
            return response
        set_scanner_cookie(response, cookie)
        return session_payload(cookie)

    @app.post("/api/scanner/activate")
    def activate(body: ScannerActivationBody, request: Request):
        enabled()
        same_origin(request)
        address = request.client.host if request.client else "unknown"
        app.state.scanner_limiter.consume_activation(address)
        scanner_browser.verify_activation(body.activation_code.get_secret_value())
        cookie = scanner_browser.issue()
        response = JSONResponse(session_payload(cookie))
        set_scanner_cookie(response, cookie)
        return response

    @app.post("/api/scanner/read")
    def read(body: ScannerReadBody, browser_hash=Depends(browser_context)):
        return scan_response(app.state.scanner.read(browser_hash, body.request_id, body.token.get_secret_value()))

    @app.post("/api/scanner/visitors")
    def visitors(body: ScannerVisitorBody, browser_hash=Depends(browser_context)):
        return scan_response(app.state.scanner.record(
            browser_hash, body.request_id, body.token.get_secret_value(), body.visitor_details.model_dump(),
        ))

    @app.get("/api/scanner/requests/{request_id}/result")
    def result(request_id: UUID, browser_hash=Depends(browser_context)):
        return app.state.scanner.result(browser_hash, request_id)

    @app.get("/api/scanner/recovery")
    def recovery(browser_hash=Depends(browser_context)):
        return app.state.scanner.latest_request(browser_hash)

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "scanner"
    if frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        target = frontend / "index.html"
        if not target.is_file():
            raise DomainError("FRONTEND_BUILD_REQUIRED")
        return FileResponse(target, media_type="text/html", headers={"Cache-Control": "no-store"})

    return app
