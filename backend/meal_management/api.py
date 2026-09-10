import hmac
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Path as ApiPath, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api_schemas import ActiveInput, AuthorizationInput, BulkEmployeesInput, CatalogInput, DepartmentInput, EmailApprovalInput, EmailProcessInput, EmployeeInput, EmployeeUpdate, ExpiryInput, LoginInput, RevokeInput, ScanBody, ScanReadBody, ScannerInput, StaffInput
from .api_common import database_readiness, scan_response
from .application import create_services
from .delivery import LocalEmailPreview
from .development_seed import DevelopmentSeedService
from .email_lifecycle import EmailLifecycle
from .email_actions import EmailActions
from .errors import ConfigurationError, DomainError
from .http_security import DatabaseLoginLimiter, SecurityMiddleware, csrf_token, error_body, status_for
from .models import ScanInput, ServerContext
from .queries import QueryService
from .runtime import RuntimeSettings
from .scan_receipts import ScanReceiptService
from .storage import storage_from_settings


Limit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[int, Query(ge=0, le=2**64 - 1)]
PathIdentifier = Annotated[int, ApiPath(gt=0, le=2**64 - 1)]


def create_app(services=None, runtime=None, queries=None, storage=None, limiter=None, email_actions=None):
    runtime = (RuntimeSettings.from_env() if runtime is None else runtime).for_application("admin")
    services = create_services() if services is None else services
    if runtime.environment == "production" and not services.database.settings.db_ssl_ca:
        raise ConfigurationError("PRODUCTION_DATABASE_TLS_REQUIRED")
    email_lifecycle = EmailLifecycle(runtime, services)

    @asynccontextmanager
    async def lifespan(application):
        email_lifecycle.start()
        try:
            yield
        finally:
            await email_lifecycle.stop()

    app = FastAPI(title="Meal management", version="0.2.0", docs_url=None, redoc_url=None, openapi_url=None, debug=False, lifespan=lifespan)
    app.state.services = services
    app.state.runtime = runtime
    app.state.email_lifecycle = email_lifecycle
    app.state.email_actions = EmailActions(runtime, services) if email_actions is None else email_actions
    app.state.queries = QueryService(services.database) if queries is None else queries
    app.state.storage = storage_from_settings(runtime) if storage is None else storage
    app.state.limiter = DatabaseLoginLimiter(services.database, runtime) if limiter is None else limiter
    app.state.scan_receipts = ScanReceiptService(services.database)
    development_enabled = runtime.environment == "development" and runtime.email_backend == "preview" and not runtime.email_send_enabled
    app.state.development_seed = (
        DevelopmentSeedService(services.database, runtime, services.qr.vault, services.qr.renderer)
        if development_enabled else None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(runtime.allowed_hosts))
    app.add_middleware(SecurityMiddleware, settings=runtime)
    seed_cookie = "__Host-meal_csrf_seed" if runtime.cookie_secure else "meal_csrf_seed"

    def context(request):
        token = request.cookies.get(runtime.session_cookie)
        if token is None:
            raise DomainError("AUTHENTICATION_REQUIRED")
        return ServerContext(token, idle_timeout_seconds=runtime.session_idle_minutes * 60)

    def csrf_guard(request: Request):
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        seed = request.cookies.get(seed_cookie)
        supplied = request.headers.get("x-csrf-token", "")
        if not seed or len(seed) > 128 or len(supplied) != 64:
            raise DomainError("CSRF_REJECTED")
        expected = csrf_token(runtime.csrf_secret, seed, request.cookies.get(runtime.session_cookie, ""))
        if not hmac.compare_digest(expected, supplied):
            raise DomainError("CSRF_REJECTED")

    def current(request: Request):
        identity = services.staff.current(context(request))
        request.state.identity = identity
        return identity

    def admin(request: Request, identity=Depends(current)):
        if "ADMIN" not in identity["roles"]:
            raise DomainError("ROLE_REQUIRED")
        csrf_guard(request)
        return context(request)

    def waiter(request: Request, identity=Depends(current)):
        if not {"ADMIN", "WAITER"}.intersection(identity["roles"]):
            raise DomainError("ROLE_REQUIRED")
        csrf_guard(request)
        return context(request)

    def authenticated(request: Request, identity=Depends(current)):
        csrf_guard(request)
        return context(request)

    def set_seed(response, session=""):
        seed = secrets.token_urlsafe(32)
        response.set_cookie(seed_cookie, seed, httponly=True, secure=runtime.cookie_secure, samesite="strict", path="/", max_age=28800)
        return csrf_token(runtime.csrf_secret, seed, session)

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        headers = None
        if error.code == "RATE_LIMITED":
            headers = {"Retry-After": str(runtime.login_window_seconds)}
        return JSONResponse(error_body(error.code, getattr(request.state, "request_id", None)), status_code=status_for(error.code), headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        if request.url.path in {"/api/scans", "/api/scan-app/read"}:
            try:
                csrf_guard(request)
                body = error.body if isinstance(error.body, dict) else {}
                await run_in_threadpool(services.meals.record_invalid, context(request), body.get("scanner_code"))
            except DomainError as failure:
                if status_for(failure.code) in {401, 403}:
                    return await domain_error(request, failure)
                return JSONResponse(error_body("SERVICE_UNAVAILABLE", request.state.request_id), status_code=503)
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
        return database_readiness(services.database)

    @app.get("/api/application")
    def application():
        return {"scanner_url": runtime.scanner_origin}

    @app.get("/api/auth/csrf")
    def csrf(request: Request, response: Response):
        seed = request.cookies.get(seed_cookie)
        if not seed or len(seed) > 128:
            value = set_seed(response, request.cookies.get(runtime.session_cookie, ""))
        else:
            value = csrf_token(runtime.csrf_secret, seed, request.cookies.get(runtime.session_cookie, ""))
        return {"csrf_token": value}

    @app.post("/api/auth/login", dependencies=[Depends(csrf_guard)])
    def login(body: LoginInput, request: Request, response: Response):
        address = request.client.host if request.client else "unknown"
        app.state.limiter.consume(body.email, address)
        session = services.staff.authenticate(body.email, body.password.get_secret_value())
        identity = services.staff.current(session)
        response.set_cookie(runtime.session_cookie, session.session_token, httponly=True, secure=runtime.cookie_secure,
                            samesite="strict", path="/", max_age=services.staff.session_hours * 3600)
        identity = dict(identity)
        identity["csrf_token"] = set_seed(response, session.session_token)
        return identity

    @app.get("/api/auth/me")
    def me(identity=Depends(current)):
        return identity

    @app.post("/api/auth/logout", dependencies=[Depends(csrf_guard)])
    def logout(request: Request, response: Response):
        token = request.cookies.get(runtime.session_cookie)
        if token:
            services.staff.logout(ServerContext(token, idle_timeout_seconds=runtime.session_idle_minutes * 60))
        response.delete_cookie(runtime.session_cookie, path="/", secure=runtime.cookie_secure, httponly=True, samesite="strict")
        response.delete_cookie(seed_cookie, path="/", secure=runtime.cookie_secure, httponly=True, samesite="strict")
        response.status_code = 204
        return response

    @app.get("/api/catalog")
    def catalog(ctx=Depends(authenticated), identity=Depends(current)):
        return {
            **app.state.queries.catalog(ctx),
            "development_test_data": development_enabled and "ADMIN" in identity["roles"],
        }

    def development_data():
        if not development_enabled:
            raise DomainError("DEVELOPMENT_TEST_DATA_NOT_FOUND")
        return app.state.development_seed

    @app.get("/api/development/test-data")
    def development_manifest(ctx=Depends(admin)):
        return development_data().manifest(ctx)

    @app.get("/api/development/test-data/qrs/{qr_id}")
    def development_qr(qr_id: PathIdentifier, ctx=Depends(admin)):
        svg = development_data().export_svg(ctx, qr_id)
        return Response(svg, media_type="image/svg+xml", headers={
            "Content-Disposition": 'inline; filename="TEST-QR-' + str(qr_id) + '.svg"',
        })

    @app.post("/api/catalog/departments", status_code=201)
    def department_create(body: DepartmentInput, ctx=Depends(admin)):
        return {"id": services.employees.create_department(ctx, body.name)}

    @app.post("/api/catalog/locations", status_code=201)
    def location_create(body: CatalogInput, ctx=Depends(admin)):
        return {"id": services.catalog.create_location(ctx, body.code, body.name)}

    @app.post("/api/catalog/meal-types", status_code=201)
    def meal_type_create(body: CatalogInput, ctx=Depends(admin)):
        return {"id": services.catalog.create_meal_type(ctx, body.code, body.name)}

    @app.post("/api/catalog/scanners", status_code=201)
    def scanner_create(body: ScannerInput, ctx=Depends(admin)):
        return {"id": services.catalog.create_scanner(ctx, body.code, body.name, body.location_id)}

    @app.get("/api/staff")
    def staff_list(limit: Limit = 50, after_id: Cursor = 0, ctx=Depends(admin)):
        return app.state.queries.staff(ctx, limit=limit, after_id=after_id)

    @app.post("/api/staff", status_code=201)
    def staff_create(body: StaffInput, ctx=Depends(admin)):
        return {"staff_id": services.staff.create_staff(ctx, body.display_name, body.email, body.password.get_secret_value(), body.roles)}

    @app.patch("/api/staff/{staff_id}/active")
    def staff_active(staff_id: PathIdentifier, body: ActiveInput, ctx=Depends(admin)):
        services.staff.set_active(ctx, staff_id, body.is_active)
        return {"staff_id": staff_id, "is_active": body.is_active}

    @app.get("/api/employees")
    def employees(limit: Limit = 50, after_id: Cursor = 0, q: Annotated[str | None, Query(max_length=150)] = None,
                  active: bool | None = None, ctx=Depends(admin)):
        return app.state.queries.list_employees(ctx, limit=limit, after_id=after_id, q=q, active=active)

    @app.post("/api/employees", status_code=201)
    def employee_create(body: EmployeeInput, ctx=Depends(admin)):
        return asdict(services.employees.register(ctx, **body.model_dump()))

    @app.post("/api/employees/bulk", status_code=201)
    def employee_bulk_create(body: BulkEmployeesInput, ctx=Depends(admin)):
        return services.employees.register_bulk(ctx, str(body.request_id), [item.model_dump() for item in body.employees])

    @app.get("/api/employees/{employee_id}")
    def employee_get(employee_id: PathIdentifier, ctx=Depends(admin)):
        return app.state.queries.employee(ctx, employee_id)

    @app.patch("/api/employees/{employee_id}")
    def employee_update(employee_id: PathIdentifier, body: EmployeeUpdate, ctx=Depends(admin)):
        values = body.model_dump(exclude_unset=True)
        if any(value is None and name != "selfie_object_key" for name, value in values.items()):
            raise DomainError("INVALID_INPUT")
        services.employees.update(ctx, employee_id, **values)
        return app.state.queries.employee(ctx, employee_id)

    @app.delete("/api/employees/{employee_id}")
    def employee_deactivate(employee_id: PathIdentifier, ctx=Depends(admin)):
        services.employees.set_active(ctx, employee_id, False)
        return {"employee_id": employee_id, "is_active": False}

    @app.post("/api/employees/{employee_id}/activate")
    def employee_activate(employee_id: PathIdentifier, ctx=Depends(admin)):
        services.employees.set_active(ctx, employee_id, True)
        return {"employee_id": employee_id, "is_active": True}

    def employee_credential(ctx, employee_id):
        credential = app.state.queries.current_employee_qr(ctx, employee_id)
        if credential is None:
            raise DomainError("QR_NOT_FOUND")
        return credential

    def qr_response(ctx, qr_id):
        metadata = app.state.queries.qr_metadata(ctx, qr_id)
        try:
            svg = services.qr.export_svg(ctx, qr_id)
        except DomainError as error:
            if error.code not in {"QR_REVOKED", "QR_EXPIRED", "EMPLOYEE_INACTIVE"}:
                raise
            return {**metadata, "qr_id": qr_id, "svg": None, "credential_status": error.code}
        return {**metadata, "qr_id": qr_id, "svg": svg, "credential_status": "ACTIVE"}

    @app.get("/api/employees/{employee_id}/qr")
    def employee_qr(employee_id: PathIdentifier, ctx=Depends(admin)):
        credential = employee_credential(ctx, employee_id)
        return qr_response(ctx, credential["id"])

    @app.post("/api/employees/{employee_id}/qr", status_code=201)
    def employee_qr_issue(employee_id: PathIdentifier, body: ExpiryInput, ctx=Depends(admin)):
        issued = services.qr.issue_employee(ctx, employee_id, body.expires_at)
        return {"employee_id": employee_id, "qr_id": issued.qr_id}

    @app.post("/api/employees/{employee_id}/qr/replace")
    def employee_qr_replace(employee_id: PathIdentifier, body: ExpiryInput, ctx=Depends(admin)):
        issued = services.qr.replace_employee(ctx, employee_id, body.expires_at)
        return {"employee_id": employee_id, "qr_id": issued.qr_id}

    @app.post("/api/employees/{employee_id}/qr/revoke")
    def employee_qr_revoke(employee_id: PathIdentifier, body: RevokeInput, ctx=Depends(admin)):
        credential = employee_credential(ctx, employee_id)
        services.qr.revoke(ctx, credential["id"], body.reason)
        return {"qr_id": credential["id"], "revoked": True}

    @app.post("/api/employees/{employee_id}/qr/resend", status_code=202)
    def employee_qr_resend(employee_id: PathIdentifier, ctx=Depends(admin)):
        email_id = services.qr.resend_employee(ctx, employee_id)
        return services.email_queue.status(ctx, email_id, employee_id=employee_id)

    @app.post("/api/employees/{employee_id}/emails/{email_id}/send")
    def employee_email_send(employee_id: PathIdentifier, email_id: PathIdentifier, ctx=Depends(admin)):
        return app.state.email_actions.send_single(ctx, employee_id, email_id)

    @app.get("/api/employees/{employee_id}/emails/{email_id}")
    def employee_email_status(employee_id: PathIdentifier, email_id: PathIdentifier, ctx=Depends(admin)):
        return services.email_queue.status(ctx, email_id, employee_id=employee_id)

    @app.post("/api/employees/{employee_id}/photo", status_code=201)
    async def employee_photo_upload(employee_id: PathIdentifier, request: Request, ctx=Depends(admin)):
        data = await request.body()
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()

        def save():
            app.state.queries.employee(ctx, employee_id)
            stored = app.state.storage.put(data, content_type)
            try:
                services.employees.update(ctx, employee_id, selfie_object_key=stored.key)
            except DomainError:
                app.state.storage.delete(stored.key)
                raise
            return {"employee_id": employee_id, "selfie_object_key": stored.key}

        return await run_in_threadpool(save)

    @app.get("/api/employees/{employee_id}/photo")
    def employee_photo(employee_id: PathIdentifier, ctx=Depends(admin)):
        employee = app.state.queries.employee(ctx, employee_id)
        if not employee["selfie_object_key"]:
            raise DomainError("PHOTO_NOT_FOUND")
        data, content_type = app.state.storage.read(employee["selfie_object_key"])
        return Response(data, media_type=content_type)

    @app.get("/api/master-qrs")
    def master_list(limit: Limit = 50, after_id: Cursor = 0, ctx=Depends(admin)):
        return app.state.queries.master_qrs(ctx, limit=limit, after_id=after_id)

    @app.post("/api/master-qrs", status_code=201)
    def master_create(body: ExpiryInput, ctx=Depends(admin)):
        issued = services.qr.issue_master(ctx, body.expires_at)
        return qr_response(ctx, issued.qr_id)

    def master_check(ctx, qr_id):
        metadata = app.state.queries.qr_metadata(ctx, qr_id)
        if metadata["kind"] != "MASTER":
            raise DomainError("MASTER_QR_REQUIRED")
        return metadata

    @app.get("/api/master-qrs/{qr_id}")
    def master_get(qr_id: PathIdentifier, ctx=Depends(admin)):
        master_check(ctx, qr_id)
        return qr_response(ctx, qr_id)

    @app.post("/api/master-qrs/{qr_id}/replace")
    def master_replace(qr_id: PathIdentifier, body: ExpiryInput, ctx=Depends(admin)):
        issued = services.qr.replace_master(ctx, qr_id, body.expires_at)
        return qr_response(ctx, issued.qr_id)

    @app.post("/api/master-qrs/{qr_id}/revoke")
    def master_revoke(qr_id: PathIdentifier, body: RevokeInput, ctx=Depends(admin)):
        master_check(ctx, qr_id)
        services.qr.revoke(ctx, qr_id, body.reason)
        return {"qr_id": qr_id, "revoked": True}

    @app.post("/api/visitor-authorizations", status_code=201)
    def visitor_authorize(body: AuthorizationInput, ctx=Depends(admin)):
        master_check(ctx, body.master_qr_id)
        credential = services.qr.retrieve(ctx, body.master_qr_id)
        values = body.model_dump(exclude={"master_qr_id"})
        authorization_id = services.approvals.authorize(ctx, master_token=credential.token, **values)
        return {"authorization_id": authorization_id, "request_id": str(body.request_id)}

    @app.get("/api/visitor-authorizations")
    def visitor_list(limit: Limit = 50, after_id: Cursor = 0, status: Literal["pending", "all"] = "pending", ctx=Depends(authenticated)):
        return app.state.queries.visitor_authorizations(ctx, limit=limit, after_id=after_id, status=status)

    @app.post("/api/visitor-authorizations/{authorization_id}/revoke")
    def visitor_revoke(authorization_id: PathIdentifier, ctx=Depends(admin)):
        services.approvals.revoke(ctx, authorization_id)
        return {"authorization_id": authorization_id, "revoked": True}

    @app.post("/api/scan-app/read")
    def scan_read(body: ScanReadBody, ctx=Depends(waiter)):
        values = body.model_dump(exclude={"token"})
        return scan_response(services.meals.read(ctx, ScanInput(token=body.token.get_secret_value(), **values)))

    @app.post("/api/scans")
    def scan(body: ScanBody, ctx=Depends(waiter)):
        values = body.model_dump(exclude={"token"})
        return scan_response(services.meals.record(ctx, ScanInput(token=body.token.get_secret_value(), **values)))

    @app.get("/api/scans/{request_id}/receipt")
    def scan_receipt(request_id: UUID, ctx=Depends(waiter)):
        return app.state.scan_receipts.get(ctx, request_id)

    @app.get("/api/scans/{request_id}/result")
    def scan_result(request_id: UUID, ctx=Depends(waiter)):
        return app.state.scan_receipts.result(ctx, request_id)

    @app.get("/api/scans/{request_id}/photo")
    def scan_photo(request_id: UUID, ctx=Depends(waiter)):
        key = app.state.scan_receipts.photo_key(ctx, request_id)
        data, content_type = app.state.storage.read(key)
        return Response(data, media_type=content_type)

    @app.get("/api/reports/meals")
    def meal_history(start: datetime, end: datetime, limit: Limit = 50, after_id: Cursor = 0,
                     employee_id: Annotated[int | None, Query(gt=0, le=2**64 - 1)] = None, ctx=Depends(admin)):
        return app.state.queries.meal_history(ctx, start, end, limit=limit, after_id=after_id, employee_id=employee_id)

    @app.get("/api/reports/totals")
    def totals(start: datetime, end: datetime, ctx=Depends(admin)):
        return services.reports.meal_report(ctx, start, end)

    @app.get("/api/reports/scans")
    def scans(start: datetime, end: datetime, limit: Limit = 50, after_id: Cursor = 0,
              outcome: Literal["RECEIVED", "SUCCESS", "REJECTED", "AWAITING_DETAILS"] | None = None, ctx=Depends(admin)):
        return app.state.queries.scans(ctx, start, end, limit=limit, after_id=after_id, outcome=outcome)

    @app.get("/api/email-settings")
    def email_settings(ctx=Depends(admin)):
        return {
            "backend": runtime.email_backend,
            "sending_enabled": runtime.email_send_enabled,
            "preview_available": runtime.environment == "development" and runtime.email_backend == "preview",
            **app.state.email_lifecycle.snapshot(),
        }

    @app.get("/api/email-queue")
    def emails(limit: Limit = 50, after_id: Cursor = 0, employee_id: Annotated[int | None, Query(gt=0, le=2**64 - 1)] = None,
               bulk_batch_id: Annotated[int | None, Query(gt=0, le=2**64 - 1)] = None, ctx=Depends(admin)):
        filters = {} if bulk_batch_id is None else {"bulk_batch_id": bulk_batch_id}
        return app.state.queries.email_status(ctx, limit=limit, after_id=after_id, employee_id=employee_id,
                                             delivery_scope="all" if employee_id is not None else "bulk", **filters)

    @app.post("/api/email-queue/approve")
    def email_approve(body: EmailApprovalInput, ctx=Depends(admin)):
        return app.state.email_actions.approve_bulk(ctx, body.email_ids)

    @app.post("/api/email-queue/process")
    def email_process(body: EmailProcessInput, ctx=Depends(admin)):
        return app.state.email_actions.process_bulk(ctx, body.email_ids)

    @app.get("/api/email-queue/{email_id}")
    def email_status(email_id: PathIdentifier, ctx=Depends(admin)):
        return services.email_queue.status(ctx, email_id)

    @app.get("/api/email-queue/{email_id}/preview", response_class=HTMLResponse)
    def email_preview(email_id: PathIdentifier, ctx=Depends(admin)):
        if runtime.email_backend != "preview" or runtime.environment != "development":
            raise DomainError("EMAIL_PREVIEW_DISABLED")
        preview = services.email_queue.preview(ctx, email_id)
        return HTMLResponse(LocalEmailPreview().render(preview))

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "admin"
    if frontend.is_dir():
        app.mount("/assets", StaticFiles(directory=frontend), name="assets")

    @app.get("/", response_class=HTMLResponse)
    def index():
        target = frontend / "index.html"
        if not target.is_file():
            raise DomainError("FRONTEND_BUILD_REQUIRED")
        return FileResponse(target, media_type="text/html", headers={"Cache-Control": "no-store"})

    return app
