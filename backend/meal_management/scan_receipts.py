from datetime import timezone

from .auth import require_scan_actor
from .errors import DomainError
from .meals import request_uuid


class ScanReceiptService:
    def __init__(self, db):
        self.db = db

    def _request(self, tx, actor, identifier):
        request = tx.one(
            "SELECT r.status, r.rejection_code FROM serving_requests r WHERE r.id = %s AND ("
            "EXISTS (SELECT 1 FROM servings s WHERE s.request_id = r.id AND s.waiter_id = %s) "
            "OR EXISTS (SELECT 1 FROM visitor_authorizations a WHERE a.request_id = r.id AND a.waiter_id = %s) "
            "OR EXISTS (SELECT 1 FROM scan_attempts a WHERE a.request_id = r.id "
            "AND a.payload_hash = r.payload_hash AND a.staff_id = %s))",
            (identifier.bytes, actor.staff_id, actor.staff_id, actor.staff_id),
        )
        if request is None:
            raise DomainError("SCAN_RECEIPT_NOT_FOUND")
        return request

    def _read(self, context, request_id):
        identifier = request_uuid(request_id)
        with self.db.transaction() as tx:
            actor = require_scan_actor(tx, context)
            request = self._request(tx, actor, identifier)
            if request["status"] == "PENDING":
                raise DomainError("SCAN_PENDING")
            if request["status"] == "REJECTED":
                raise DomainError("SCAN_REJECTED")
            if request["status"] != "SUCCEEDED":
                raise DomainError("PROCESSING_UNCONFIRMED")
            row = tx.one(
                "SELECT s.id AS serving_id, s.waiter_id, s.kind, s.quantity, s.served_at, s.employee_id, "
                "e.full_name AS employee_name, e.selfie_object_key, COALESCE(s.visitor_name, a.visitor_name) AS visitor_name, "
                "(SELECT COUNT(*) FROM meals m WHERE m.serving_id = s.id) AS meal_count "
                "FROM servings s JOIN serving_requests r ON r.id = s.request_id "
                "LEFT JOIN employees e ON e.id = s.employee_id "
                "LEFT JOIN visitor_authorizations a ON a.id = s.authorization_id "
                "AND a.request_id = s.request_id AND a.waiter_id = s.waiter_id "
                "WHERE s.request_id = %s AND s.waiter_id = %s AND r.status = 'SUCCEEDED'",
                (identifier.bytes, actor.staff_id),
            )
            if row is None or row["waiter_id"] != actor.staff_id:
                raise DomainError("SCAN_RECEIPT_NOT_FOUND")
            if row["quantity"] < 1 or row["meal_count"] != row["quantity"]:
                raise DomainError("PROCESSING_UNCONFIRMED")
            if row["kind"] not in {"EMPLOYEE", "MASTER"}:
                raise DomainError("PROCESSING_UNCONFIRMED")
            if row["kind"] == "MASTER" and row["visitor_name"] is None:
                raise DomainError("PROCESSING_UNCONFIRMED")
        return identifier, row

    def get(self, context, request_id):
        identifier, row = self._read(context, request_id)
        served_at = row["served_at"]
        served_at = served_at.replace(tzinfo=timezone.utc) if served_at.tzinfo is None else served_at.astimezone(timezone.utc)
        employee = row["kind"] == "EMPLOYEE"
        return {
            "request_id": str(identifier),
            "serving_id": row["serving_id"],
            "kind": row["kind"],
            "quantity": row["quantity"],
            "served_at": served_at,
            "employee_name": row["employee_name"] if employee else None,
            "employee_id": row["employee_id"] if employee else None,
            "photo_available": employee and bool(row["selfie_object_key"]),
            "visitor_name": row["visitor_name"] if not employee else None,
        }

    def photo_key(self, context, request_id):
        _, row = self._read(context, request_id)
        if row["kind"] != "EMPLOYEE" or not row["selfie_object_key"]:
            raise DomainError("PHOTO_NOT_FOUND")
        return row["selfie_object_key"]

    def result(self, context, request_id):
        identifier = request_uuid(request_id)
        result = {
            "approved": False,
            "code": "PROCESSING_UNCONFIRMED",
            "request_id": str(identifier),
            "serving_id": None,
            "meal_ids": [],
            "duplicate": True,
        }
        with self.db.transaction() as tx:
            actor = require_scan_actor(tx, context)
            request = self._request(tx, actor, identifier)
            if request["status"] == "REJECTED":
                result["code"] = request["rejection_code"] or "PROCESSING_UNCONFIRMED"
            elif request["status"] == "SUCCEEDED":
                serving = tx.one(
                    "SELECT s.id, s.waiter_id, s.quantity, s.kind FROM servings s "
                    "JOIN serving_requests r ON r.id = s.request_id "
                    "WHERE s.request_id = %s AND s.waiter_id = %s AND r.status = 'SUCCEEDED'",
                    (identifier.bytes, actor.staff_id),
                )
                if serving is None or serving["waiter_id"] != actor.staff_id:
                    raise DomainError("SCAN_RECEIPT_NOT_FOUND")
                meals = tx.all(
                    "SELECT id, unit_number, serving_quantity FROM meals WHERE serving_id = %s ORDER BY unit_number",
                    (serving["id"],),
                )
                quantity = serving["quantity"]
                complete = (
                    serving["kind"] in {"EMPLOYEE", "MASTER"}
                    and quantity > 0
                    and len(meals) == quantity
                    and len({meal["id"] for meal in meals}) == quantity
                    and [meal["unit_number"] for meal in meals] == list(range(1, quantity + 1))
                    and all(meal["serving_quantity"] == quantity for meal in meals)
                )
                if complete:
                    result.update(
                        approved=True,
                        code="APPROVED",
                        serving_id=serving["id"],
                        meal_ids=[meal["id"] for meal in meals],
                    )
        return result
