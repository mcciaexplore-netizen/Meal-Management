from datetime import datetime, timezone
from uuid import UUID

from .auth import require_actor
from .database import is_postgres
from .email_queue import RESEND_LIMIT, email_delivery_state
from .errors import DomainError
from .meals import EFFECTIVE_MEAL_TYPE, positive_integer
from .paging import newest_first, next_cursor, parse_cursor
from .reports import _date_range, scanner_phone_join
from .security import required_text


def _pagination(limit, before):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise DomainError("INVALID_REPORT_LIMIT")
    return parse_cursor(before)


def _where(filters, params, time_column, id_column, cursor):
    condition, values, order = newest_first(time_column, id_column, cursor)
    if condition is not None:
        filters.append(condition)
        params.extend(values)
    return order


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


def _page(rows, limit, time_key):
    return {"items": [_record(row) for row in rows[:limit]], "next_cursor": next_cursor(rows, limit, time_key)}


def _email_state(items):
    for item in items:
        if "email_status" in item:
            status, error = item.pop("email_status"), item.pop("email_error", None)
            item["email_status"], item["email_code"] = (None, None) if status is None else email_delivery_state(
                {"status": status, "last_error": error})
            item["resends_left"] = max(0, RESEND_LIMIT + 1 - (item.pop("email_count", 0) or 0)) if item.get("email") else 0
    return items


def _master_meal_types(tx, items):
    identifiers = [item["id"] for item in items if item.get("kind") == "MASTER"]
    by_qr = {identifier: [] for identifier in identifiers}
    if identifiers:
        placeholders = ", ".join(["%s"] * len(identifiers))
        for row in tx.all(
            "SELECT a.qr_id, a.meal_type_id, t.code, t.name, t.is_active, a.meal_limit, a.meals_used, "
            "a.meal_limit - a.meals_used AS meals_remaining, a.exhausted_at "
            "FROM master_qr_meal_types a JOIN meal_types t ON t.id = a.meal_type_id "
            f"WHERE a.qr_id IN ({placeholders}) ORDER BY a.qr_id, t.id",
            tuple(identifiers),
        ):
            record = _record(row)
            by_qr[record.pop("qr_id")].append(record)
    for item in items:
        if item.get("kind") == "MASTER":
            item["meal_types"] = by_qr[item["id"]]
    return items


class QueryService:
    def __init__(self, db):
        self.db = db

    def staff(self, context, limit=50, before=None):
        cursor = _pagination(limit, before)
        filters, params = ["s.is_scanner = 0"], []
        order = _where(filters, params, "s.created_at", "s.id", cursor)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            roles = (
                "STRING_AGG(r.role_code, ',' ORDER BY r.role_code)" if is_postgres(tx)
                else "GROUP_CONCAT(r.role_code ORDER BY r.role_code SEPARATOR ',')"
            )
            rows = tx.all(
                "SELECT s.id, s.display_name, s.email, s.is_active, s.created_at, "
                + roles + " AS role_codes "
                "FROM staff_accounts s LEFT JOIN staff_account_roles r ON r.staff_id = s.id "
                "WHERE " + " AND ".join(filters) + " GROUP BY s.id, s.display_name, s.email, s.is_active, s.created_at"
                + order,
                (*params, limit + 1),
            )
        result = _page(rows, limit, "created_at")
        for item in result["items"]:
            roles = item.pop("role_codes", None)
            item["roles"] = roles.split(",") if roles else []
        return result

    def list_employees(self, context, limit=50, before=None, q=None, active=None):
        cursor = _pagination(limit, before)
        filters = ["NOT EXISTS (SELECT 1 FROM employee_archives x WHERE x.employee_id = e.id)"]
        params = []
        if q is not None:
            term = required_text(q, "SEARCH", 150)
            term = term.replace("=", "==").replace("%", "=%").replace("_", "=_")
            filters.append("(e.employee_code LIKE %s ESCAPE '=' OR e.full_name LIKE %s ESCAPE '=' OR e.email LIKE %s ESCAPE '=' OR e.company_name LIKE %s ESCAPE '=' OR e.phone LIKE %s ESCAPE '=')")
            params.extend(["%" + term + "%"] * 5)
        if active is not None:
            if type(active) is not bool:
                raise DomainError("INVALID_ACTIVE_FILTER")
            filters.append("e.is_active = %s")
            params.append(active)
        order = _where(filters, params, "e.created_at", "e.id", cursor)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT e.id, e.employee_code, e.full_name, e.email, e.company_name, e.phone, e.department_id, "
                "d.name AS department_name, e.is_active, e.selfie_object_key, e.created_at, e.updated_at "
                "FROM employees e JOIN departments d ON d.id = e.department_id WHERE "
                + " AND ".join(filters) + order,
                params,
            )
        return _page(rows, limit, "created_at")

    def employee(self, context, employee_id):
        positive_integer(employee_id, "EMPLOYEE_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT e.id, e.employee_code, e.full_name, e.email, e.company_name, e.phone, e.department_id, "
                "d.name AS department_name, e.is_active, e.selfie_object_key, e.created_at, e.updated_at "
                "FROM employees e JOIN departments d ON d.id = e.department_id WHERE e.id = %s "
                "AND NOT EXISTS (SELECT 1 FROM employee_archives x WHERE x.employee_id = e.id)",
                (employee_id,),
            )
            if row is None:
                raise DomainError("EMPLOYEE_NOT_FOUND")
            meal_types = tx.all(
                "SELECT meal_type_id FROM employee_meal_types WHERE employee_id = %s ORDER BY meal_type_id", (employee_id,),
            )
        record = _record(row)
        record["meal_type_ids"] = [item["meal_type_id"] for item in meal_types]
        return record

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

    def master_qrs(self, context, limit=50, before=None):
        cursor = _pagination(limit, before)
        filters, params = ["q.kind = 'MASTER'"], []
        order = _where(filters, params, "q.issued_at", "q.id", cursor)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT q.id, q.id AS qr_id, q.kind, q.issued_by, q.issued_at, q.expires_at, q.revoked_at, "
                "q.revoked_by, q.revocation_reason, a.company_name, a.contact_name, a.email, a.phone, "
                "a.department_id, vd.name AS department_name, a.event_name, a.batch_id, a.batch_row, "
                "(SELECT eq.status FROM email_queue eq WHERE eq.qr_id = q.id ORDER BY eq.id DESC LIMIT 1) AS email_status, "
                "(SELECT eq.last_error FROM email_queue eq WHERE eq.qr_id = q.id ORDER BY eq.id DESC LIMIT 1) AS email_error, "
                "(SELECT COUNT(*) FROM email_queue eq WHERE eq.qr_id = q.id) AS email_count, "
                "a.meal_limit, a.meals_used, a.meal_limit - a.meals_used AS meals_remaining, a.exhausted_at "
                "FROM qr_credentials q LEFT JOIN master_qr_allocations a ON a.qr_id = q.id "
                "LEFT JOIN departments vd ON vd.id = a.department_id "
                "WHERE " + " AND ".join(filters) + order,
                (*params, limit + 1),
            )
            page = _page(rows, limit, "issued_at")
            _master_meal_types(tx, _email_state(page["items"]))
        return page

    def qr_metadata(self, context, qr_id):
        positive_integer(qr_id, "QR_ID")
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT q.id, q.id AS qr_id, q.kind, q.employee_id, q.issued_by, q.issued_at, q.expires_at, "
                "q.revoked_at, q.revoked_by, q.revocation_reason, a.company_name, a.contact_name, a.email, "
                "a.phone, a.meal_limit, a.meals_used, a.meal_limit - a.meals_used AS meals_remaining, "
                "a.department_id, vd.name AS department_name, a.event_name, a.batch_id, a.batch_row, "
                "(SELECT eq.status FROM email_queue eq WHERE eq.qr_id = q.id ORDER BY eq.id DESC LIMIT 1) AS email_status, "
                "(SELECT eq.last_error FROM email_queue eq WHERE eq.qr_id = q.id ORDER BY eq.id DESC LIMIT 1) AS email_error, "
                "(SELECT COUNT(*) FROM email_queue eq WHERE eq.qr_id = q.id) AS email_count, "
                "a.exhausted_at FROM qr_credentials q LEFT JOIN master_qr_allocations a ON a.qr_id = q.id "
                "LEFT JOIN departments vd ON vd.id = a.department_id "
                "WHERE q.id = %s",
                (qr_id,),
            )
            if row is None:
                raise DomainError("QR_NOT_FOUND")
            record = _master_meal_types(tx, _email_state([_record(row)]))[0]
        return record

    def email_status(self, context, limit=50, before=None, employee_id=None, delivery_scope="all", bulk_batch_id=None):
        cursor = _pagination(limit, before)
        if delivery_scope not in {"all", "bulk"}:
            raise DomainError("INVALID_EMAIL_DELIVERY_SCOPE")
        filters = ["1 = 1"]
        params = []
        if delivery_scope == "bulk":
            filters.append("q.delivery_mode IN ('BULK', 'LEGACY')")
        if employee_id is not None:
            filters.append("c.employee_id = %s")
            params.append(positive_integer(employee_id, "EMPLOYEE_ID"))
        if bulk_batch_id is not None:
            filters.append("q.bulk_batch_id = %s")
            params.append(positive_integer(bulk_batch_id, "EMAIL_BATCH_ID"))
        order = _where(filters, params, "q.created_at", "q.id", cursor)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT q.id, q.qr_id, c.employee_id, c.kind AS qr_kind, COALESCE(e.full_name, va.contact_name) AS employee_name, "
                "q.recipient_email, q.status, q.created_at, q.updated_at, "
                "q.delivery_mode, q.bulk_batch_id, q.approved_by_staff_id, q.approved_at, "
                "q.last_error "
                "FROM email_queue q JOIN qr_credentials c ON c.id = q.qr_id "
                "LEFT JOIN employees e ON e.id = c.employee_id "
                "LEFT JOIN master_qr_allocations va ON va.qr_id = c.id WHERE "
                + " AND ".join(filters) + order,
                params,
            )
        for row in rows:
            row["status"], row["code"] = email_delivery_state(row)
            row["last_error"] = row["code"]
        return _page(rows, limit, "created_at")

    def meal_history(self, context, start, end, limit=50, before=None, employee_id=None, meal_type_id=None, department_id=None):
        cursor = _pagination(limit, before)
        start_utc, end_utc = _date_range(start, end)
        filters = [
            "m.served_at >= %s", "m.served_at < %s",
            "NOT EXISTS (SELECT 1 FROM meal_voids v WHERE v.meal_id = m.id)",
        ]
        params = [start_utc, end_utc]
        if employee_id is not None:
            filters.append("s.employee_id = %s")
            params.append(positive_integer(employee_id, "EMPLOYEE_ID"))
        if meal_type_id is not None:
            filters.append(EFFECTIVE_MEAL_TYPE + " = %s")
            params.append(positive_integer(meal_type_id, "MEAL_TYPE_ID"))
        if department_id is not None:
            filters.append("COALESCE(e.department_id, ma.department_id) = %s")
            params.append(positive_integer(department_id, "DEPARTMENT_ID"))
        order = _where(filters, params, "m.served_at", "m.id", cursor)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT m.id, m.id AS meal_id, m.served_at, m.unit_number, s.id AS serving_id, "
                "s.request_id, s.kind, s.quantity AS serving_quantity, s.employee_id, "
                "COALESCE(e.department_id, ma.department_id) AS department_id, md.name AS department_name, "
                "ma.event_name, "
                "e.employee_code, e.full_name AS employee_name, e.company_name AS employee_company_name, "
                "e.email AS employee_email, e.phone AS employee_phone, t.id AS meal_type_id, "
                "s.meal_type_id AS recorded_meal_type_id, ot.name AS recorded_meal_type_name, "
                "(SELECT COUNT(*) FROM meal_type_corrections c WHERE c.meal_id = m.id) AS correction_count, "
                "t.code AS meal_type_code, t.name AS meal_type_name, s.waiter_id, "
                "COALESCE(ph.label, w.display_name) AS waiter_name, ph.id AS scanner_phone_id, "
                "s.location_id, l.name AS location_name, "
                "s.scanner_id, d.code AS scanner_code, "
                "COALESCE(s.visitor_company_name, a.visitor_organization) AS visitor_company_name, "
                "COALESCE(s.visitor_name, a.visitor_name) AS visitor_name, "
                "s.visitor_email, s.visitor_phone, a.visitor_organization, "
                "a.visit_purpose, a.authorized_by, admin.display_name AS admin_name "
                "FROM meals m JOIN servings s ON s.id = m.serving_id "
                "LEFT JOIN employees e ON e.id = s.employee_id "
                "LEFT JOIN master_qr_allocations ma ON ma.qr_id = s.qr_id AND s.kind = 'MASTER' "
                "LEFT JOIN departments md ON md.id = COALESCE(e.department_id, ma.department_id) "
                "JOIN meal_types t ON t.id = " + EFFECTIVE_MEAL_TYPE + " "
                "JOIN meal_types ot ON ot.id = s.meal_type_id "
                "JOIN staff_accounts w ON w.id = s.waiter_id "
                + scanner_phone_join("s.request_id") +
                "JOIN locations l ON l.id = s.location_id "
                "JOIN scanner_devices d ON d.id = s.scanner_id "
                "LEFT JOIN visitor_authorizations a ON a.id = s.authorization_id "
                "LEFT JOIN staff_accounts admin ON admin.id = a.authorized_by WHERE "
                + " AND ".join(filters) + order,
                params,
            )
        return _page(rows, limit, "served_at")

    def scans(self, context, start, end, limit=50, before=None, outcome=None):
        cursor = _pagination(limit, before)
        start_utc, end_utc = _date_range(start, end)
        filters = ["a.received_at >= %s", "a.received_at < %s"]
        params = [start_utc, end_utc]
        if outcome is not None:
            if outcome not in {"RECEIVED", "SUCCESS", "REJECTED", "AWAITING_DETAILS"}:
                raise DomainError("INVALID_SCAN_OUTCOME")
            filters.append("a.outcome = %s")
            params.append(outcome)
        order = _where(filters, params, "a.received_at", "a.id", cursor)
        params.append(limit + 1)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            rows = tx.all(
                "SELECT a.id, a.request_id, a.qr_id, a.staff_id, s.display_name AS staff_name, "
                "COALESCE(ph.label, s.display_name) AS waiter_name, ph.id AS scanner_phone_id, "
                "a.scanner_id, a.reported_scanner_code AS scanner_code, a.location_id, "
                "l.name AS location_name, a.outcome, a.serving_id, a.rejection_code, "
                "a.received_at, a.completed_at FROM scan_attempts a "
                "LEFT JOIN staff_accounts s ON s.id = a.staff_id "
                + scanner_phone_join("a.request_id") +
                "LEFT JOIN locations l ON l.id = a.location_id WHERE "
                + " AND ".join(filters) + order,
                params,
            )
        return _page(rows, limit, "received_at")

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
            shared = tx.one(
                "SELECT p.scanner_id, p.meal_type_id, d.location_id FROM scan_app_settings p "
                "JOIN scanner_devices d ON d.id = p.scanner_id WHERE p.id = 1"
            ) if admin else None
        result = {key: [_record(row) for row in rows] for key, rows in {
            "departments": departments, "meal_types": meal_types, "locations": locations,
            "scanners": scanners, "waiters": waiters,
        }.items()}
        if admin:
            result["shared"] = {
                key: (None if shared is None else shared[key]) for key in ("scanner_id", "meal_type_id", "location_id")
            }
        return result

    def visitor_authorizations(self, context, limit=50, before=None, status="pending"):
        cursor = _pagination(limit, before)
        if status not in {"pending", "all"}:
            raise DomainError("INVALID_AUTHORIZATION_STATUS")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN", "WAITER"})
            filters = ["1 = 1"]
            params = []
            if "ADMIN" not in actor.roles:
                filters.append("a.waiter_id = %s")
                params.append(actor.staff_id)
            if status == "pending":
                filters.extend(["r.status = 'PENDING'", "a.revoked_at IS NULL", "a.expires_at > %s"])
                params.append(tx.now())
            order = _where(filters, params, "a.authorized_at", "a.id", cursor)
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
                + " AND ".join(filters) + order,
                params,
            )
        return _page(rows, limit, "authorized_at")
