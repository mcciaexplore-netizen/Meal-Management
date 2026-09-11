from datetime import datetime, timezone
from uuid import UUID

from .auth import require_actor
from .email_queue import email_delivery_state
from .errors import DomainError
from .meals import positive_integer
from .reports import _date_range
from .security import required_text


def _pagination(limit, after_id):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise DomainError("INVALID_REPORT_LIMIT")
    if type(after_id) is not int or not 0 <= after_id <= 2**64 - 1:
        raise DomainError("INVALID_REPORT_CURSOR")


def _record(row):
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        elif key == "request_id" and value is not None:
            result[key] = str(UUID(bytes=bytes(value))) if isinstance(value, (bytes, bytearray)) else str(UUID(str(value)))
        elif key == "is_active":
            result[key] = bool(value)
    return result


def _page(rows, limit):
    items = [_record(row) for row in rows[:limit]]
    return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}


class QueryService:
    def __init__(self, db):
        self.db = db

    def staff(self, context, limit=50, after_id=0):
        _pagination(limit, after_id)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT s.id, s.display_name, s.email, s.is_active, "
                "GROUP_CONCAT(r.role_code ORDER BY r.role_code SEPARATOR ',') AS role_codes "
                "FROM staff_accounts s LEFT JOIN staff_account_roles r ON r.staff_id = s.id "
                "WHERE s.id > %s GROUP BY s.id, s.display_name, s.email, s.is_active "
                "ORDER BY s.id LIMIT %s",
                (after_id, limit + 1),
            )
        result = _page(rows, limit)
        for item in result["items"]:
            roles = item.pop("role_codes", None)
            item["roles"] = roles.split(",") if roles else []
        return result

    def list_employees(self, context, limit=50, after_id=0, q=None, active=None):
        _pagination(limit, after_id)
        filters = ["e.id > %s", "NOT EXISTS (SELECT 1 FROM employee_archives x WHERE x.employee_id = e.id)"]
        params = [after_id]
        if q is not None:
            term = required_text(q, "SEARCH", 150)
            term = term.replace("=", "==").replace("%", "=%").replace("_", "=_")
            filters.append("(e.employee_code LIKE %s ESCAPE '=' OR e.full_name LIKE %s ESCAPE '=' OR e.email LIKE %s ESCAPE '=')")
            params.extend(["%" + term + "%"] * 3)
        if active is not None:
            if type(active) is not bool:
                raise DomainError("INVALID_ACTIVE_FILTER")
            filters.append("e.is_active = %s")
            params.append(active)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT e.id, e.employee_code, e.full_name, e.email, e.department_id, "
                "d.name AS department_name, e.is_active, e.selfie_object_key, e.created_at, e.updated_at "
                "FROM employees e JOIN departments d ON d.id = e.department_id WHERE "
                + " AND ".join(filters) + " ORDER BY e.id LIMIT %s",
                params,
            )
        return _page(rows, limit)

    def employee(self, context, employee_id):
        positive_integer(employee_id, "EMPLOYEE_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT e.id, e.employee_code, e.full_name, e.email, e.department_id, "
                "d.name AS department_name, e.is_active, e.selfie_object_key, e.created_at, e.updated_at "
                "FROM employees e JOIN departments d ON d.id = e.department_id WHERE e.id = %s "
                "AND NOT EXISTS (SELECT 1 FROM employee_archives x WHERE x.employee_id = e.id)",
                (employee_id,),
            )
            if row is None:
                raise DomainError("EMPLOYEE_NOT_FOUND")
        return _record(row)

    def current_employee_qr(self, context, employee_id):
        positive_integer(employee_id, "EMPLOYEE_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            if tx.one(
                "SELECT e.id FROM employees e WHERE e.id = %s "
                "AND NOT EXISTS (SELECT 1 FROM employee_archives x WHERE x.employee_id = e.id)",
                (employee_id,),
            ) is None:
                raise DomainError("EMPLOYEE_NOT_FOUND")
            row = tx.one(
                "SELECT id, id AS qr_id, kind, employee_id, issued_by, issued_at, expires_at, "
                "revoked_at, revoked_by, revocation_reason FROM qr_credentials "
                "WHERE employee_id = %s AND kind = 'EMPLOYEE' AND revoked_at IS NULL",
                (employee_id,),
            )
        return _record(row) if row is not None else None

    def master_qrs(self, context, limit=50, after_id=0):
        _pagination(limit, after_id)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT id, id AS qr_id, kind, issued_by, issued_at, expires_at, revoked_at, "
                "revoked_by, revocation_reason FROM qr_credentials "
                "WHERE kind = 'MASTER' AND id > %s ORDER BY id LIMIT %s",
                (after_id, limit + 1),
            )
        return _page(rows, limit)

    def qr_metadata(self, context, qr_id):
        positive_integer(qr_id, "QR_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT id, id AS qr_id, kind, employee_id, issued_by, issued_at, expires_at, "
                "revoked_at, revoked_by, revocation_reason FROM qr_credentials WHERE id = %s",
                (qr_id,),
            )
            if row is None:
                raise DomainError("QR_NOT_FOUND")
        return _record(row)

    def email_status(self, context, limit=50, after_id=0, employee_id=None, delivery_scope="all", bulk_batch_id=None):
        _pagination(limit, after_id)
        if delivery_scope not in {"all", "bulk"}:
            raise DomainError("INVALID_EMAIL_DELIVERY_SCOPE")
        filters = ["q.id > %s"]
        params = [after_id]
        if delivery_scope == "bulk":
            filters.append("q.delivery_mode IN ('BULK', 'LEGACY')")
        if employee_id is not None:
            filters.append("c.employee_id = %s")
            params.append(positive_integer(employee_id, "EMPLOYEE_ID"))
        if bulk_batch_id is not None:
            filters.append("q.bulk_batch_id = %s")
            params.append(positive_integer(bulk_batch_id, "EMAIL_BATCH_ID"))
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT q.id, q.qr_id, c.employee_id, e.full_name AS employee_name, "
                "q.recipient_email, q.status, q.created_at, q.updated_at, "
                "q.delivery_mode, q.bulk_batch_id, q.approved_by_staff_id, q.approved_at, "
                "q.last_error "
                "FROM email_queue q JOIN qr_credentials c ON c.id = q.qr_id "
                "JOIN employees e ON e.id = c.employee_id WHERE "
                + " AND ".join(filters) + " ORDER BY q.id LIMIT %s",
                params,
            )
        for row in rows:
            row["status"], row["code"] = email_delivery_state(row)
            row["last_error"] = row["code"]
        return _page(rows, limit)

    def meal_history(self, context, start, end, limit=50, after_id=0, employee_id=None):
        _pagination(limit, after_id)
        start_utc, end_utc = _date_range(start, end)
        filters = [
            "m.served_at >= %s", "m.served_at < %s", "m.id > %s",
            "NOT EXISTS (SELECT 1 FROM meal_voids v WHERE v.meal_id = m.id)",
        ]
        params = [start_utc, end_utc, after_id]
        if employee_id is not None:
            filters.append("s.employee_id = %s")
            params.append(positive_integer(employee_id, "EMPLOYEE_ID"))
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT m.id, m.id AS meal_id, m.served_at, m.unit_number, s.id AS serving_id, "
                "s.request_id, s.kind, s.quantity AS serving_quantity, s.employee_id, "
                "e.employee_code, e.full_name AS employee_name, s.meal_type_id, "
                "t.code AS meal_type_code, t.name AS meal_type_name, s.waiter_id, "
                "w.display_name AS waiter_name, s.location_id, l.name AS location_name, "
                "s.scanner_id, d.code AS scanner_code, "
                "COALESCE(s.visitor_company_name, a.visitor_organization) AS visitor_company_name, "
                "COALESCE(s.visitor_name, a.visitor_name) AS visitor_name, "
                "s.visitor_email, s.visitor_phone, a.visitor_organization, "
                "a.visit_purpose, a.authorized_by, admin.display_name AS admin_name "
                "FROM meals m JOIN servings s ON s.id = m.serving_id "
                "LEFT JOIN employees e ON e.id = s.employee_id "
                "JOIN meal_types t ON t.id = s.meal_type_id "
                "JOIN staff_accounts w ON w.id = s.waiter_id "
                "JOIN locations l ON l.id = s.location_id "
                "JOIN scanner_devices d ON d.id = s.scanner_id "
                "LEFT JOIN visitor_authorizations a ON a.id = s.authorization_id "
                "LEFT JOIN staff_accounts admin ON admin.id = a.authorized_by WHERE "
                + " AND ".join(filters) + " ORDER BY m.id LIMIT %s",
                params,
            )
        return _page(rows, limit)

    def scans(self, context, start, end, limit=50, after_id=0, outcome=None):
        _pagination(limit, after_id)
        start_utc, end_utc = _date_range(start, end)
        filters = ["a.received_at >= %s", "a.received_at < %s", "a.id > %s"]
        params = [start_utc, end_utc, after_id]
        if outcome is not None:
            if outcome not in {"RECEIVED", "SUCCESS", "REJECTED", "AWAITING_DETAILS"}:
                raise DomainError("INVALID_SCAN_OUTCOME")
            filters.append("a.outcome = %s")
            params.append(outcome)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT a.id, a.request_id, a.qr_id, a.staff_id, s.display_name AS staff_name, "
                "a.scanner_id, a.reported_scanner_code AS scanner_code, a.location_id, "
                "l.name AS location_name, a.outcome, a.serving_id, a.rejection_code, "
                "a.received_at, a.completed_at FROM scan_attempts a "
                "LEFT JOIN staff_accounts s ON s.id = a.staff_id "
                "LEFT JOIN locations l ON l.id = a.location_id WHERE "
                + " AND ".join(filters) + " ORDER BY a.id LIMIT %s",
                params,
            )
        return _page(rows, limit)

    def catalog(self, context):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN", "WAITER"})
            admin = "ADMIN" in actor.roles
            condition = "" if admin else " WHERE is_active = 1"
            departments = tx.all("SELECT id, name, is_active FROM departments ORDER BY name") if admin else []
            meal_types = tx.all("SELECT id, code, name, is_active FROM meal_types" + condition + " ORDER BY name")
            locations = tx.all("SELECT id, code, name, is_active FROM locations" + condition + " ORDER BY name")
            scanners = tx.all(
                "SELECT d.id, d.code, d.name, d.location_id, l.name AS location_name, d.is_active "
                "FROM scanner_devices d JOIN locations l ON l.id = d.location_id"
                + ("" if admin else " WHERE d.is_active = 1 AND l.is_active = 1") + " ORDER BY d.name"
            )
            waiters = tx.all(
                "SELECT DISTINCT s.id, s.display_name FROM staff_accounts s "
                "JOIN staff_account_roles r ON r.staff_id = s.id "
                "WHERE s.is_active = 1 AND r.role_code IN ('ADMIN', 'WAITER') ORDER BY s.display_name"
            ) if admin else []
        return {key: [_record(row) for row in rows] for key, rows in {
            "departments": departments, "meal_types": meal_types, "locations": locations,
            "scanners": scanners, "waiters": waiters,
        }.items()}

    def visitor_authorizations(self, context, limit=50, after_id=0, status="pending"):
        _pagination(limit, after_id)
        if status not in {"pending", "all"}:
            raise DomainError("INVALID_AUTHORIZATION_STATUS")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN", "WAITER"})
            filters = ["a.id > %s"]
            params = [after_id]
            if "ADMIN" not in actor.roles:
                filters.append("a.waiter_id = %s")
                params.append(actor.staff_id)
            if status == "pending":
                filters.extend(["r.status = 'PENDING'", "a.revoked_at IS NULL", "a.expires_at > %s"])
                params.append(tx.now())
            params.append(limit + 1)
            rows = tx.all(
                "SELECT a.id, a.id AS authorization_id, a.request_id, a.qr_id AS master_qr_id, "
                "a.visitor_name, a.visitor_organization, a.visit_purpose, a.quantity, a.meal_type_id, "
                "t.name AS meal_type_name, a.waiter_id, w.display_name AS waiter_name, "
                "a.scanner_id, d.code AS scanner_code, a.location_id, l.name AS location_name, "
                "a.authorized_by, admin.display_name AS admin_name, a.authorized_at, a.expires_at, "
                "a.revoked_at, r.status AS request_status FROM visitor_authorizations a "
                "JOIN serving_requests r ON r.id = a.request_id "
                "JOIN meal_types t ON t.id = a.meal_type_id "
                "JOIN staff_accounts w ON w.id = a.waiter_id "
                "JOIN staff_accounts admin ON admin.id = a.authorized_by "
                "JOIN scanner_devices d ON d.id = a.scanner_id "
                "JOIN locations l ON l.id = a.location_id WHERE "
                + " AND ".join(filters) + " ORDER BY a.id LIMIT %s",
                params,
            )
        return _page(rows, limit)
