import hmac
import re
import time
from datetime import timedelta
from uuid import UUID

from .auth import audit, require_actor, require_scan_actor
from .database import retry_transaction
from .errors import DomainError
from .models import ScanAppContext, ScanInput, ScanResult
from .security import normalize_email, normalize_phone, payload_digest, required_text, token_digest, utc_naive


class AttemptRejection(DomainError):
    pass


def request_uuid(value):
    try:
        result = value if isinstance(value, UUID) else UUID(str(value))
        if result.int == 0:
            raise ValueError
        return result
    except (ValueError, AttributeError, TypeError):
        raise DomainError("INVALID_REQUEST_ID") from None


def positive_integer(value, name, maximum=2**64 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise DomainError("INVALID_" + name)
    return value


def positive_identifiers(values, name):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise DomainError("INVALID_" + name)
    identifiers = tuple(positive_integer(value, name[:-1]) for value in values)
    if len(set(identifiers)) != len(identifiers):
        raise DomainError("INVALID_" + name)
    return tuple(sorted(identifiers))


def normalize_visitor_details(value):
    if not isinstance(value, dict) or set(value) != {"company_name", "name", "email", "phone"}:
        raise DomainError("INVALID_VISITOR_DETAILS")
    company_name = required_text(value["company_name"], "VISITOR_COMPANY_NAME", 150)
    name = required_text(value["name"], "VISITOR_NAME", 150)
    email = normalize_email(value["email"])
    phone = normalize_phone(value["phone"], "VISITOR_PHONE")
    return {"company_name": company_name, "name": name, "email": email, "phone": phone}


def scan_fingerprint(digest, actor_id, scanner_id, location_id, meal_type_id, quantity):
    return payload_digest({
        "token_hash": digest.hex(),
        "staff_id": actor_id,
        "scanner_id": scanner_id,
        "location_id": location_id,
        "meal_type_id": meal_type_id,
        "quantity": quantity,
    })


def lock_scanner(tx, code):
    row = tx.one(
        "SELECT d.id, d.location_id, d.is_active, l.is_active AS location_active "
        "FROM scanner_devices d JOIN locations l ON l.id = d.location_id "
        "WHERE d.code = %s FOR UPDATE", (code,),
    )
    if row is None:
        raise DomainError("UNKNOWN_SCANNER")
    if not row["is_active"]:
        raise DomainError("SCANNER_INACTIVE")
    if not row["location_active"]:
        raise DomainError("LOCATION_INACTIVE")
    return row


def check_qr(qr, now):
    if qr is None:
        raise DomainError("UNKNOWN_QR")
    if qr["revoked_at"] is not None:
        raise DomainError("QR_REVOKED")
    if qr["expires_at"] is not None and qr["expires_at"] <= now:
        raise DomainError("QR_EXPIRED")


def check_meal_type(tx, meal_type_id):
    row = tx.one("SELECT id, is_active FROM meal_types WHERE id = %s FOR SHARE", (meal_type_id,))
    if not row or not row["is_active"]:
        raise DomainError("MEAL_TYPE_UNAVAILABLE")


def reserve_request(tx, identifier, fingerprint):
    tx.execute(
        "INSERT INTO serving_requests (id, payload_hash, status) VALUES (%s, %s, 'PENDING') "
        "ON DUPLICATE KEY UPDATE id = id",
        (identifier.bytes, fingerprint),
    )
    return tx.one("SELECT * FROM serving_requests WHERE id = %s FOR UPDATE", (identifier.bytes,))


class ApprovalService:
    def __init__(self, db):
        self.db = db

    @retry_transaction
    def authorize(self, context, request_id, master_token, waiter_id, scanner_code,
                  meal_type_id, quantity, visitor_name, visitor_organization=None,
                  visit_purpose=None, expires_at=None):
        identifier = request_uuid(request_id)
        digest = token_digest(master_token)
        waiter_id = positive_integer(waiter_id, "WAITER_ID")
        meal_type_id = positive_integer(meal_type_id, "MEAL_TYPE_ID")
        quantity = positive_integer(quantity, "QUANTITY", 65535)
        scanner_code = required_text(scanner_code, "SCANNER_CODE", 64)
        visitor_name = required_text(visitor_name, "VISITOR_NAME", 150)
        if visitor_organization is not None:
            visitor_organization = required_text(visitor_organization, "VISITOR_ORGANIZATION", 150)
        if visit_purpose is not None:
            visit_purpose = required_text(visit_purpose, "VISIT_PURPOSE", 255)
        expiry = None if expires_at is None else utc_naive(expires_at)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            waiter = tx.one("SELECT id, is_active FROM staff_accounts WHERE id = %s FOR UPDATE", (waiter_id,))
            waiter_role = tx.one(
                "SELECT role_code FROM staff_account_roles WHERE staff_id = %s "
                "AND role_code IN ('ADMIN', 'WAITER') FOR SHARE",
                (waiter_id,),
            )
            if not waiter or not waiter["is_active"] or not waiter_role:
                raise DomainError("WAITER_UNAVAILABLE")
            scanner = lock_scanner(tx, scanner_code)
            check_meal_type(tx, meal_type_id)
            qr = tx.one("SELECT * FROM qr_credentials WHERE token_hash = %s FOR UPDATE", (digest,))
            now = tx.now()
            check_qr(qr, now)
            if qr["kind"] != "MASTER":
                raise DomainError("MASTER_QR_REQUIRED")
            expiry = now + timedelta(minutes=10) if expiry is None else expiry
            if not now < expiry <= now + timedelta(hours=1):
                raise DomainError("INVALID_AUTHORIZATION_EXPIRY")
            fingerprint = scan_fingerprint(digest, waiter_id, scanner["id"], scanner["location_id"], meal_type_id, quantity)
            request = reserve_request(tx, identifier, fingerprint)
            if not hmac.compare_digest(bytes(request["payload_hash"]), fingerprint):
                raise DomainError("IDEMPOTENCY_KEY_REUSED")
            if request["status"] != "PENDING":
                raise DomainError("REQUEST_ALREADY_FINALIZED")
            existing = tx.one("SELECT id FROM visitor_authorizations WHERE request_id = %s", (identifier.bytes,))
            if existing:
                raise DomainError("AUTHORIZATION_ALREADY_EXISTS")
            authorization_id = tx.insert(
                "INSERT INTO visitor_authorizations "
                "(request_id, qr_id, kind, meal_type_id, location_id, quantity, waiter_id, scanner_id, "
                "visitor_name, visitor_organization, visit_purpose, authorized_by, authorized_at, expires_at) "
                "VALUES (%s, %s, 'MASTER', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (identifier.bytes, qr["id"], meal_type_id, scanner["location_id"], quantity, waiter_id,
                 scanner["id"], visitor_name, visitor_organization, visit_purpose, actor.staff_id, now, expiry),
            )
            audit(tx, actor.staff_id, "VISITOR_AUTHORIZED", "visitor_authorizations", authorization_id,
                  after={"request_id": str(identifier), "quantity": quantity, "waiter_id": waiter_id, "expires_at": expiry})
        return authorization_id

    @retry_transaction
    def revoke(self, context, authorization_id):
        positive_integer(authorization_id, "AUTHORIZATION_ID")
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT * FROM visitor_authorizations WHERE id = %s FOR UPDATE", (authorization_id,))
            if not row:
                raise DomainError("AUTHORIZATION_NOT_FOUND")
            if row["revoked_at"] is None:
                tx.execute("UPDATE visitor_authorizations SET revoked_at = %s WHERE id = %s", (tx.now(), authorization_id))
                audit(tx, actor.staff_id, "AUTHORIZATION_REVOKED", "visitor_authorizations", authorization_id)


class MealService:
    def __init__(self, db, transaction_attempts=3):
        self.db = db
        self.transaction_attempts = positive_integer(transaction_attempts, "TRANSACTION_ATTEMPTS", 10)

    @retry_transaction
    def record_invalid(self, context, scanner_code=None):
        code = scanner_code.strip() if isinstance(scanner_code, str) and len(scanner_code) <= 64 else None
        with self.db.transaction() as tx:
            actor = require_scan_actor(tx, context)
            scanner = tx.one("SELECT id, location_id FROM scanner_devices WHERE code = %s", (code,)) if code else None
            now = tx.now()
            tx.insert(
                "INSERT INTO scan_attempts (staff_id, scanner_id, location_id, reported_scanner_code, "
                "outcome, rejection_code, received_at, completed_at) "
                "VALUES (%s, %s, %s, %s, 'REJECTED', 'INVALID_INPUT', %s, %s)",
                (actor.staff_id, scanner["id"] if scanner else None, scanner["location_id"] if scanner else None,
                 code, now, now),
            )
        return ScanResult(False, "INVALID_INPUT", None)

    def record(self, context, scan):
        return self._record(context, scan)

    def read(self, context, scan):
        return self._record(context, scan, reading=True)

    def _record(self, context, scan, reading=False):
        if not isinstance(scan, ScanInput):
            raise DomainError("INVALID_SCAN_INPUT")
        for attempt in range(self.transaction_attempts):
            try:
                receipt = self._receive(context, scan, reading=reading) if reading else self._receive(context, scan)
                break
            except DomainError:
                raise
            except Exception as error:
                if getattr(error, "errno", None) in {1205, 1213} and attempt + 1 < self.transaction_attempts:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                raise DomainError("SCAN_RECEIPT_UNCONFIRMED") from None
        if receipt["error"] is not None:
            return ScanResult(False, receipt["error"], receipt["request_text"])
        for attempt in range(self.transaction_attempts):
            try:
                return self._process(context, scan, receipt)
            except Exception as error:
                if getattr(error, "errno", None) in {1205, 1213} and attempt + 1 < self.transaction_attempts:
                    time.sleep(0.02 * (attempt + 1))
                    continue
                self._mark_interrupted(receipt["attempt_id"])
                return ScanResult(False, "PROCESSING_UNCONFIRMED", receipt["request_text"])
        return ScanResult(False, "PROCESSING_UNCONFIRMED", receipt["request_text"])

    def _receive(self, context, scan, reading=False):
        identifier = None
        digest = None
        fingerprint = None
        scanner = None
        actor = None
        error = None
        response_error = None
        visitor_details = None
        visitor_hash = None
        code = scan.scanner_code[:64] if isinstance(scan.scanner_code, str) else None
        with self.db.transaction() as tx:
            try:
                actor = require_scan_actor(tx, context)
                identifier = request_uuid(scan.request_id)
                digest = token_digest(scan.token)
                positive_integer(scan.meal_type_id, "MEAL_TYPE_ID")
                positive_integer(scan.quantity, "QUANTITY", 65535)
                if scan.authorization_id is not None:
                    positive_integer(scan.authorization_id, "AUTHORIZATION_ID")
                if reading and (scan.quantity != 1 or scan.authorization_id is not None or scan.visitor_details is not None):
                    raise DomainError("INVALID_SCAN_READ_INPUT")
                if scan.visitor_details is not None:
                    visitor_details = normalize_visitor_details(scan.visitor_details)
                    visitor_hash = payload_digest(visitor_details)
                    if scan.authorization_id is not None:
                        raise DomainError("VISITOR_DETAILS_AUTHORIZATION_CONFLICT")
                    if scan.quantity != 1:
                        raise DomainError("VISITOR_QUANTITY_MUST_BE_ONE")
                code = required_text(scan.scanner_code, "SCANNER_CODE", 64)
            except DomainError as exc:
                error = exc.code
        with self.db.transaction() as tx:
            if code is not None:
                scanner = tx.one("SELECT id, location_id FROM scanner_devices WHERE code = %s", (code,))
            linked_id = None
            if error is not None and actor is not None and identifier is not None:
                existing = tx.one("SELECT id FROM serving_requests WHERE id = %s", (identifier.bytes,))
                if existing is not None:
                    response_error = "REQUEST_MISMATCH"
            if error is None:
                fingerprint = scan_fingerprint(digest, actor.staff_id, scanner["id"] if scanner else None,
                                               scanner["location_id"] if scanner else None, scan.meal_type_id, scan.quantity)
                request = reserve_request(tx, identifier, fingerprint)
                linked_id = identifier.bytes
                if request["status"] in {"SUCCEEDED", "REJECTED"}:
                    original = tx.one(
                        "SELECT scanner_id, location_id, reported_scanner_code FROM scan_attempts "
                        "WHERE request_id = %s AND payload_hash = %s ORDER BY id LIMIT 1",
                        (identifier.bytes, request["payload_hash"]),
                    )
                    if original is None:
                        error = "IDEMPOTENCY_PROOF_UNAVAILABLE"
                    elif original["reported_scanner_code"] != code:
                        error = "IDEMPOTENCY_KEY_REUSED"
                    else:
                        fingerprint = scan_fingerprint(
                            digest, actor.staff_id, original["scanner_id"], original["location_id"],
                            scan.meal_type_id, scan.quantity,
                        )
            now = tx.now()
            attempt_id = tx.insert(
                "INSERT INTO scan_attempts (request_id, payload_hash, token_hash, staff_id, scanner_id, "
                "location_id, reported_scanner_code, outcome, rejection_code, received_at, completed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (linked_id, fingerprint, digest, actor.staff_id if actor else None,
                 scanner["id"] if scanner else None, scanner["location_id"] if scanner else None,
                 code, "REJECTED" if error else "RECEIVED", error, now, now if error else None),
            )
        return {"attempt_id": attempt_id, "request_id": linked_id,
                "request_text": str(identifier) if identifier else None,
                "fingerprint": fingerprint, "digest": digest, "error": response_error or error,
                "staff_id": actor.staff_id if actor else None,
                "scanner_id": scanner["id"] if scanner else None,
                "location_id": scanner["location_id"] if scanner else None,
                "visitor_details": visitor_details, "visitor_hash": visitor_hash, "reading": reading,
                "public_scanner": isinstance(context, ScanAppContext)}

    def _finish_attempt(self, tx, receipt, code, serving_id=None, qr_id=None):
        changed = tx.execute(
            "UPDATE scan_attempts SET outcome = %s, rejection_code = %s, serving_id = %s, "
            "qr_id = %s, completed_at = %s WHERE id = %s AND outcome = 'RECEIVED'",
            ("SUCCESS" if code == "APPROVED" else "REJECTED", None if code == "APPROVED" else code,
             serving_id, qr_id, tx.now(), receipt["attempt_id"]),
        )
        if changed != 1:
            raise DomainError("ATTEMPT_ALREADY_FINALIZED")

    def _bind_scan(self, tx, scan, receipt):
        tx.execute(
            "UPDATE serving_requests SET scan_authorization_id = %s, scan_visitor_hash = %s, "
            "scan_bound_at = %s WHERE id = %s AND scan_bound_at IS NULL",
            (scan.authorization_id, receipt.get("visitor_hash"), tx.now(), receipt["request_id"]),
        )

    def _visitor_proof_matches(self, request, receipt):
        previous = request.get("scan_visitor_hash")
        supplied = receipt.get("visitor_hash")
        if previous is None or supplied is None:
            return previous is None and supplied is None
        return hmac.compare_digest(bytes(previous), supplied)

    def _process(self, context, scan, receipt):
        defer_binding = receipt.get("reading") or (
            scan.authorization_id is None and receipt.get("visitor_details") is None
        )
        with self.db.transaction() as tx:
            request = tx.one("SELECT * FROM serving_requests WHERE id = %s FOR UPDATE", (receipt["request_id"],))
            if request is None:
                raise DomainError("REQUEST_NOT_FOUND")
            attempt = tx.one("SELECT outcome FROM scan_attempts WHERE id = %s FOR UPDATE", (receipt["attempt_id"],))
            if not attempt or attempt["outcome"] != "RECEIVED":
                raise DomainError("ATTEMPT_ALREADY_FINALIZED")
            try:
                actor = require_scan_actor(tx, context)
                if actor.staff_id != receipt["staff_id"]:
                    raise DomainError("AUTHENTICATION_CHANGED")
            except DomainError as error:
                self._finish_attempt(tx, receipt, error.code)
                result = ScanResult(False, error.code, receipt["request_text"])
            else:
                if not hmac.compare_digest(bytes(request["payload_hash"]), receipt["fingerprint"]):
                    self._finish_attempt(tx, receipt, "IDEMPOTENCY_KEY_REUSED")
                    result = ScanResult(False, "IDEMPOTENCY_KEY_REUSED", receipt["request_text"])
                elif (
                    not receipt.get("reading")
                    and (
                        (
                            request.get("scan_bound_at") is not None
                            and request.get("scan_authorization_id") != scan.authorization_id
                        )
                        or (
                            (request.get("scan_bound_at") is not None or request["status"] != "PENDING")
                            and not self._visitor_proof_matches(request, receipt)
                        )
                    )
                ):
                    self._finish_attempt(tx, receipt, "IDEMPOTENCY_KEY_REUSED")
                    result = ScanResult(False, "IDEMPOTENCY_KEY_REUSED", receipt["request_text"])
                elif request["status"] == "SUCCEEDED":
                    serving = tx.one("SELECT id, qr_id, authorization_id FROM servings WHERE request_id = %s", (receipt["request_id"],))
                    if serving is None:
                        raise DomainError("INCOMPLETE_SERVING")
                    if not receipt.get("reading") and serving["authorization_id"] != scan.authorization_id:
                        self._finish_attempt(tx, receipt, "IDEMPOTENCY_KEY_REUSED")
                        result = ScanResult(False, "IDEMPOTENCY_KEY_REUSED", receipt["request_text"])
                    else:
                        rows = tx.all("SELECT id FROM meals WHERE serving_id = %s ORDER BY unit_number", (serving["id"],))
                        self._finish_attempt(tx, receipt, "DUPLICATE_REQUEST", serving["id"], serving["qr_id"])
                        result = ScanResult(True, "APPROVED", receipt["request_text"], serving["id"],
                                            tuple(row["id"] for row in rows), True)
                elif request["status"] == "REJECTED":
                    code = request["rejection_code"] if request.get("scan_bound_at") else "IDEMPOTENCY_PROOF_UNAVAILABLE"
                    self._finish_attempt(tx, receipt, code)
                    result = ScanResult(False, code, receipt["request_text"], duplicate=bool(request.get("scan_bound_at")))
                else:
                    prepared = tx.one(
                        "SELECT id FROM visitor_authorizations WHERE request_id = %s",
                        (receipt["request_id"],),
                    )
                    if prepared and prepared["id"] != scan.authorization_id:
                        self._finish_attempt(tx, receipt, "AUTHORIZATION_SCOPE_MISMATCH")
                        return ScanResult(False, "AUTHORIZATION_SCOPE_MISMATCH", receipt["request_text"])
                    if request.get("scan_bound_at") is None and not defer_binding:
                        self._bind_scan(tx, scan, receipt)
                    tx.execute("SAVEPOINT serving_decision")
                    try:
                        result = self._create_serving(tx, actor, scan, receipt)
                    except DomainError as error:
                        tx.execute("ROLLBACK TO SAVEPOINT serving_decision")
                        if not isinstance(error, AttemptRejection):
                            if request.get("scan_bound_at") is None and defer_binding:
                                self._bind_scan(tx, scan, receipt)
                            tx.execute(
                                "UPDATE serving_requests SET status = 'REJECTED', rejection_code = %s, completed_at = %s WHERE id = %s",
                                (error.code, tx.now(), receipt["request_id"]),
                            )
                        qr = tx.one("SELECT id FROM qr_credentials WHERE token_hash = %s", (receipt["digest"],))
                        self._finish_attempt(tx, receipt, error.code, qr_id=qr["id"] if qr else None)
                        result = ScanResult(False, error.code, receipt["request_text"])
        return result

    def _create_serving(self, tx, actor, scan, receipt):
        scanner = lock_scanner(tx, scan.scanner_code.strip())
        if scanner["id"] != receipt["scanner_id"] or scanner["location_id"] != receipt["location_id"]:
            raise DomainError("SCANNER_LOCATION_CHANGED")
        check_meal_type(tx, scan.meal_type_id)
        initial_qr = tx.one("SELECT id, kind, employee_id FROM qr_credentials WHERE token_hash = %s", (receipt["digest"],))
        if initial_qr is None:
            raise DomainError("UNKNOWN_QR")
        employee_id = initial_qr["employee_id"]
        if initial_qr["kind"] == "EMPLOYEE":
            employee = tx.one("SELECT id, is_active FROM employees WHERE id = %s FOR UPDATE", (employee_id,))
            if not employee or not employee["is_active"]:
                raise DomainError("EMPLOYEE_INACTIVE")
        qr = tx.one("SELECT * FROM qr_credentials WHERE id = %s FOR UPDATE", (initial_qr["id"],))
        now = tx.now()
        check_qr(qr, now)
        serving_visitor = receipt.get("visitor_details")
        if qr["kind"] == "EMPLOYEE":
            if scan.quantity != 1:
                raise DomainError("EMPLOYEE_QUANTITY_MUST_BE_ONE")
            if scan.authorization_id is not None:
                raise DomainError("AUTHORIZATION_NOT_APPLICABLE")
            if receipt.get("visitor_details") is not None:
                raise DomainError("VISITOR_DETAILS_NOT_APPLICABLE")
            self._bind_scan(tx, scan, receipt)
        elif qr["kind"] == "MASTER":
            allocation = None
            if scan.authorization_id is None and receipt.get("visitor_details") is None:
                allocation = tx.one(
                    "SELECT qr_id, company_name, contact_name, email, phone, meal_limit, meals_used, exhausted_at "
                    "FROM master_qr_allocations WHERE qr_id = %s FOR UPDATE",
                    (qr["id"],),
                )
            if allocation is not None:
                if scan.quantity != 1:
                    raise DomainError("VISITOR_QUANTITY_MUST_BE_ONE")
                if scan.authorization_id is not None or receipt.get("visitor_details") is not None:
                    raise DomainError("MASTER_ALLOCATION_SCOPE_MISMATCH")
                if allocation["exhausted_at"] is not None or allocation["meals_used"] >= allocation["meal_limit"]:
                    raise DomainError("QR_EXPIRED")
                changed = tx.execute(
                    "UPDATE master_qr_allocations SET "
                    "exhausted_at = CASE WHEN meals_used + 1 = meal_limit THEN %s ELSE NULL END, "
                    "meals_used = meals_used + 1 "
                    "WHERE qr_id = %s AND meals_used < meal_limit AND exhausted_at IS NULL",
                    (now, qr["id"]),
                )
                if changed != 1:
                    raise DomainError("QR_EXPIRED")
                serving_visitor = {
                    "company_name": allocation["company_name"],
                    "name": allocation["contact_name"],
                    "email": allocation["email"],
                    "phone": allocation["phone"],
                }
                self._bind_scan(tx, scan, receipt)
            elif receipt.get("reading"):
                changed = tx.execute(
                    "UPDATE scan_attempts SET outcome = 'AWAITING_DETAILS', qr_id = %s, "
                    "completed_at = %s WHERE id = %s AND outcome = 'RECEIVED'",
                    (qr["id"], tx.now(), receipt["attempt_id"]),
                )
                if changed != 1:
                    raise DomainError("ATTEMPT_ALREADY_FINALIZED")
                return {"kind": "MASTER", "next": "VISITOR_DETAILS", "request_id": receipt["request_text"]}
            elif receipt.get("visitor_details") is not None:
                if scan.quantity != 1:
                    raise DomainError("VISITOR_QUANTITY_MUST_BE_ONE")
                if scan.authorization_id is not None:
                    raise DomainError("VISITOR_DETAILS_AUTHORIZATION_CONFLICT")
            else:
                if scan.authorization_id is None:
                    raise AttemptRejection("QR_EXPIRED" if receipt.get("public_scanner") else "VISITOR_DETAILS_REQUIRED")
                now = self._validate_authorization(tx, actor, scan, receipt, qr, scanner)
                check_qr(qr, now)
        else:
            raise DomainError("INVALID_QR_CATEGORY")
        serving_columns = (
            "request_id, qr_id, kind, employee_id, meal_type_id, quantity, waiter_id, "
            "scanner_id, location_id, authorization_id, served_at"
        )
        serving_values = [
            receipt["request_id"], qr["id"], qr["kind"], employee_id, scan.meal_type_id, scan.quantity,
            actor.staff_id, scanner["id"], scanner["location_id"], scan.authorization_id, now,
        ]
        if serving_visitor is not None:
            visitor = serving_visitor
            serving_columns += ", visitor_company_name, visitor_name, visitor_email, visitor_phone"
            serving_values.extend(visitor[key] for key in ("company_name", "name", "email", "phone"))
        serving_id = tx.insert(
            "INSERT INTO servings (" + serving_columns + ") VALUES (" + ", ".join(["%s"] * len(serving_values)) + ")",
            tuple(serving_values),
        )
        meal_ids = []
        for unit in range(1, scan.quantity + 1):
            meal_ids.append(tx.insert(
                "INSERT INTO meals (serving_id, unit_number, serving_quantity, served_at) VALUES (%s, %s, %s, %s)",
                (serving_id, unit, scan.quantity, now),
            ))
        tx.execute("UPDATE serving_requests SET status = 'SUCCEEDED', completed_at = %s WHERE id = %s", (tx.now(), receipt["request_id"]))
        self._finish_attempt(tx, receipt, "APPROVED", serving_id, qr["id"])
        return ScanResult(True, "APPROVED", receipt["request_text"], serving_id, tuple(meal_ids))

    def _validate_authorization(self, tx, actor, scan, receipt, qr, scanner):
        prepared = tx.one("SELECT id FROM visitor_authorizations WHERE request_id = %s", (receipt["request_id"],))
        if prepared and prepared["id"] != scan.authorization_id:
            raise AttemptRejection("AUTHORIZATION_SCOPE_MISMATCH")
        if scan.authorization_id is None:
            raise DomainError("ADMIN_AUTHORIZATION_REQUIRED")
        approval = tx.one("SELECT * FROM visitor_authorizations WHERE id = %s FOR UPDATE", (scan.authorization_id,))
        if not approval:
            raise DomainError("AUTHORIZATION_NOT_FOUND")
        expected = {"request_id": receipt["request_id"], "qr_id": qr["id"], "meal_type_id": scan.meal_type_id,
                    "quantity": scan.quantity, "waiter_id": actor.staff_id, "scanner_id": scanner["id"], "location_id": scanner["location_id"]}
        if any(approval[key] != value for key, value in expected.items()):
            raise DomainError("AUTHORIZATION_SCOPE_MISMATCH")
        admin = tx.one("SELECT is_active FROM staff_accounts WHERE id = %s FOR SHARE", (approval["authorized_by"],))
        role = tx.one("SELECT role_code FROM staff_account_roles WHERE staff_id = %s AND role_code = 'ADMIN' FOR SHARE", (approval["authorized_by"],))
        if not admin or not admin["is_active"] or not role:
            raise DomainError("AUTHORIZING_ADMIN_UNAVAILABLE")
        if approval["revoked_at"] is not None:
            raise DomainError("AUTHORIZATION_REVOKED")
        now = tx.now()
        if approval["expires_at"] <= now:
            raise DomainError("AUTHORIZATION_EXPIRED")
        return now

    def _mark_interrupted(self, attempt_id):
        try:
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE scan_attempts SET outcome = 'REJECTED', rejection_code = 'PROCESSING_INTERRUPTED', "
                    "completed_at = %s WHERE id = %s AND outcome = 'RECEIVED'", (tx.now(), attempt_id),
                )
        except Exception:
            pass

    def reconcile_received(self, context, older_than, limit=100):
        cutoff = utc_naive(older_than)
        positive_integer(limit, "LIMIT", 1000)
        with self.db.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            candidates = tx.all(
                "SELECT id, request_id FROM scan_attempts WHERE outcome = 'RECEIVED' AND received_at < %s ORDER BY id LIMIT %s",
                (cutoff, limit),
            )
        changed = 0
        for candidate in candidates:
            with self.db.transaction() as tx:
                actor = require_actor(tx, context, {"ADMIN"})
                if candidate["request_id"] is not None:
                    tx.one("SELECT id FROM serving_requests WHERE id = %s FOR UPDATE", (candidate["request_id"],))
                updated = tx.execute(
                    "UPDATE scan_attempts SET outcome = 'REJECTED', rejection_code = 'PROCESSING_INTERRUPTED', "
                    "completed_at = %s WHERE id = %s AND outcome = 'RECEIVED'", (tx.now(), candidate["id"]),
                )
                if updated:
                    audit(tx, actor.staff_id, "SCAN_RECONCILED", "scan_attempts", candidate["id"],
                          after={"outcome": "REJECTED", "rejection_code": "PROCESSING_INTERRUPTED"})
                changed += updated
        return changed

    def void(self, context, meal_id, reason):
        positive_integer(meal_id, "MEAL_ID")
        reason = required_text(reason, "removal_reason", 255)
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            meal = tx.one(
                "SELECT m.id, m.serving_id, m.unit_number, m.served_at, s.kind "
                "FROM meals m JOIN servings s ON s.id = m.serving_id "
                "WHERE m.id = %s FOR UPDATE",
                (meal_id,),
            )
            if meal is None:
                raise DomainError("MEAL_NOT_FOUND")
            if tx.one(
                "SELECT meal_id FROM meal_voids WHERE meal_id = %s FOR SHARE",
                (meal_id,),
            ) is not None:
                raise DomainError("MEAL_ALREADY_REMOVED")
            voided_at = tx.now()
            tx.insert(
                "INSERT INTO meal_voids (meal_id, voided_by_staff_id, reason, voided_at) "
                "VALUES (%s, %s, %s, %s)",
                (meal_id, actor.staff_id, reason, voided_at),
            )
            audit(
                tx,
                actor.staff_id,
                "MEAL_REMOVED",
                "meals",
                meal_id,
                before={
                    "serving_id": meal["serving_id"],
                    "unit_number": meal["unit_number"],
                    "served_at": meal["served_at"],
                    "kind": meal["kind"],
                },
                after={"voided_at": voided_at, "reason": reason},
            )
        return voided_at

    def void_bulk(self, context, meal_ids, reason):
        identifiers = positive_identifiers(meal_ids, "MEAL_IDS")
        reason = required_text(reason, "removal_reason", 255)
        placeholders = ", ".join(["%s"] * len(identifiers))
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            meals = tx.all(
                "SELECT m.id, m.serving_id, m.unit_number, m.served_at, s.kind "
                "FROM meals m JOIN servings s ON s.id = m.serving_id "
                f"WHERE m.id IN ({placeholders}) ORDER BY m.id FOR UPDATE",
                identifiers,
            )
            if tuple(meal["id"] for meal in meals) != identifiers:
                raise DomainError("MEAL_NOT_FOUND")
            voids = tx.all(
                "SELECT meal_id FROM meal_voids "
                f"WHERE meal_id IN ({placeholders}) ORDER BY meal_id FOR SHARE",
                identifiers,
            )
            if voids:
                raise DomainError("MEAL_ALREADY_REMOVED")
            voided_at = tx.now()
            for meal in meals:
                meal_id = meal["id"]
                tx.insert(
                    "INSERT INTO meal_voids (meal_id, voided_by_staff_id, reason, voided_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (meal_id, actor.staff_id, reason, voided_at),
                )
                audit(
                    tx,
                    actor.staff_id,
                    "MEAL_REMOVED",
                    "meals",
                    meal_id,
                    before={
                        "serving_id": meal["serving_id"],
                        "unit_number": meal["unit_number"],
                        "served_at": meal["served_at"],
                        "kind": meal["kind"],
                    },
                    after={"voided_at": voided_at, "reason": reason, "bulk": True},
                )
        return {
            "meal_ids": list(identifiers),
            "removed_count": len(identifiers),
            "removed_at": voided_at,
        }
