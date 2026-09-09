import hmac
from contextlib import contextmanager
from uuid import UUID

from .database import retry_transaction
from .errors import DomainError
from .meals import request_uuid
from .models import ScanAppContext, ScanInput


def _browser_hash(value):
    if not isinstance(value, bytes) or len(value) != 32:
        raise DomainError("INVALID_SCANNER_BROWSER")
    return value


class ScanAppService:
    def __init__(self, database, meals, receipts):
        self.database = database
        self.meals = meals
        self.receipts = receipts

    @contextmanager
    def _transaction(self):
        try:
            with self.database.transaction() as tx:
                yield tx
        except Exception as error:
            if getattr(error, "errno", None) in {1054, 1146}:
                raise DomainError("SCANNER_NOT_CONFIGURED") from None
            raise

    def _profile(self, tx):
        profile = tx.one(
            "SELECT p.staff_id, p.scanner_id, p.meal_type_id, p.is_enabled, d.code AS scanner_code, "
            "s.is_active AS staff_active, s.is_scanner, d.is_active AS scanner_active, "
            "l.is_active AS location_active, m.is_active AS meal_type_active "
            "FROM scan_app_settings p JOIN staff_accounts s ON s.id = p.staff_id "
            "JOIN scanner_devices d ON d.id = p.scanner_id "
            "JOIN locations l ON l.id = d.location_id "
            "JOIN meal_types m ON m.id = p.meal_type_id WHERE p.id = 1 FOR SHARE"
        )
        if profile is None or not all(profile[key] for key in (
            "is_enabled", "staff_active", "is_scanner", "scanner_active", "location_active", "meal_type_active",
        )):
            raise DomainError("SCANNER_NOT_CONFIGURED")
        return profile

    def _mapping(self, tx, identifier, browser_hash, locking=False):
        row = tx.one(
            "SELECT request_id, browser_hash, staff_id, scanner_id, meal_type_id, scanner_code "
            "FROM scan_app_requests WHERE request_id = %s" + (" FOR UPDATE" if locking else ""),
            (identifier.bytes,),
        )
        if row is not None and not hmac.compare_digest(bytes(row["browser_hash"]), browser_hash):
            raise DomainError("REQUEST_MISMATCH")
        return row

    @retry_transaction
    def _reserve(self, browser_hash, request_id):
        browser_hash = _browser_hash(browser_hash)
        identifier = request_uuid(request_id)
        with self._transaction() as tx:
            row = self._mapping(tx, identifier, browser_hash, locking=True)
            if row is None:
                profile = self._profile(tx)
                row = {
                    "request_id": identifier.bytes, "browser_hash": browser_hash,
                    "staff_id": profile["staff_id"], "scanner_id": profile["scanner_id"],
                    "meal_type_id": profile["meal_type_id"], "scanner_code": profile["scanner_code"],
                }
                try:
                    tx.execute(
                        "INSERT INTO scan_app_requests "
                        "(request_id, browser_hash, staff_id, scanner_id, meal_type_id, scanner_code) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (identifier.bytes, browser_hash, row["staff_id"], row["scanner_id"],
                         row["meal_type_id"], row["scanner_code"]),
                    )
                except Exception as error:
                    if getattr(error, "errno", None) != 1062:
                        raise
                    row = self._mapping(tx, identifier, browser_hash, locking=True)
                    if row is None:
                        raise DomainError("SCAN_RECEIPT_UNCONFIRMED") from None
        return identifier, row

    def _owned(self, browser_hash, request_id):
        browser_hash = _browser_hash(browser_hash)
        identifier = request_uuid(request_id)
        with self._transaction() as tx:
            row = self._mapping(tx, identifier, browser_hash)
            if row is None:
                raise DomainError("SCAN_RECEIPT_NOT_FOUND")
        return identifier, row

    def _scan(self, identifier, row, token, visitor_details=None):
        return ScanInput(
            request_id=identifier, token=token, meal_type_id=row["meal_type_id"],
            scanner_code=row["scanner_code"], quantity=1, visitor_details=visitor_details,
        )

    def read(self, browser_hash, request_id, token):
        identifier, row = self._reserve(browser_hash, request_id)
        return self.meals.read(ScanAppContext(row["staff_id"]), self._scan(identifier, row, token))

    def record(self, browser_hash, request_id, token, visitor_details):
        identifier, row = self._reserve(browser_hash, request_id)
        return self.meals.record(
            ScanAppContext(row["staff_id"]), self._scan(identifier, row, token, visitor_details),
        )

    def result(self, browser_hash, request_id):
        identifier, row = self._owned(browser_hash, request_id)
        return self.receipts.result(ScanAppContext(row["staff_id"]), identifier)

    def latest_request(self, browser_hash):
        browser_hash = _browser_hash(browser_hash)
        with self._transaction() as tx:
            row = tx.one(
                "SELECT request_id FROM scan_app_requests WHERE browser_hash = %s "
                "ORDER BY created_at DESC, request_id DESC LIMIT 1",
                (browser_hash,),
            )
        if row is None:
            return {"request": None}
        return {"request": {"request_id": str(UUID(bytes=bytes(row["request_id"]))), "quantity": 1}}

    def record_invalid(self, browser_hash):
        _browser_hash(browser_hash)
        with self._transaction() as tx:
            profile = self._profile(tx)
        return self.meals.record_invalid(ScanAppContext(profile["staff_id"]), profile["scanner_code"])
