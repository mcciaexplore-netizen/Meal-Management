import base64
import binascii
import math
from datetime import timedelta

from .auth import audit, require_actor
from .database import retry_transaction
from .errors import DomainError
from .paging import newest_first, next_cursor, parse_cursor
from .security import required_text


NOTICE_VERSION = "2026-10-26"
SELFIE_PREFIX = "shift-selfies/"
MAX_SELFIE_BYTES = 1_000_000
EARTH_RADIUS_M = 6_371_000


def distance_m(latitude, longitude, other_latitude, other_longitude):
    phi1, phi2 = math.radians(latitude), math.radians(other_latitude)
    delta_phi = math.radians(other_latitude - latitude)
    delta_lambda = math.radians(other_longitude - longitude)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _number(value, name, minimum, maximum):
    if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
        raise DomainError("INVALID_" + name)
    return float(value)


def _id(value, name):
    if type(value) is not int or not 1 <= value <= 2**64 - 1:
        raise DomainError("INVALID_" + name)
    return value


def decode_selfie(value):
    if not isinstance(value, str) or not value or len(value) > (MAX_SELFIE_BYTES * 4) // 3 + 4:
        raise DomainError("INVALID_SHIFT_SELFIE")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise DomainError("INVALID_SHIFT_SELFIE") from None
    if not data or len(data) > MAX_SELFIE_BYTES:
        raise DomainError("INVALID_SHIFT_SELFIE")
    return data


def _summary(row, now):
    status = "ENDED" if row["ended_at"] is not None else ("ACTIVE" if row["expires_at"] > now else "EXPIRED")
    return {
        "id": row["id"], "phone_id": row["phone_id"], "phone_label": row.get("phone_label"),
        "waiter_name": row["waiter_name"], "latitude": float(row["latitude"]), "longitude": float(row["longitude"]),
        "accuracy_m": float(row["accuracy_m"]),
        "distance_m": None if row["distance_m"] is None else float(row["distance_m"]),
        "flagged": None if row["flagged"] is None else bool(row["flagged"]),
        "consent_notice_version": row["consent_notice_version"], "consent_at": row["consent_at"],
        "started_at": row["started_at"], "expires_at": row["expires_at"], "ended_at": row["ended_at"],
        "end_reason": row["end_reason"], "status": status,
    }


class WaiterShiftService:
    def __init__(self, database, storage, shift_hours=3):
        if type(shift_hours) is not int or not 1 <= shift_hours <= 12:
            raise DomainError("INVALID_SHIFT_HOURS")
        self.database = database
        self.storage = storage
        self.length = timedelta(hours=shift_hours)

    def _scanner_actor(self, tx):
        row = tx.one("SELECT staff_id FROM scan_app_settings WHERE id = 1")
        return None if row is None else row["staff_id"]

    def _open(self, tx, phone_id, now, locking=False):
        return tx.one(
            "SELECT * FROM waiter_shifts WHERE phone_id = %s AND ended_at IS NULL AND expires_at > %s "
            "ORDER BY id DESC LIMIT 1" + (" FOR UPDATE" if locking else ""),
            (phone_id, now),
        )

    def current(self, phone_id):
        phone_id = _id(phone_id, "SCANNER_PHONE_ID")
        with self.database.transaction() as tx:
            now = tx.now()
            row = self._open(tx, phone_id, now)
        return None if row is None else _summary(row, now)

    def require(self, phone_id):
        shift = self.current(phone_id)
        if shift is None:
            raise DomainError("SHIFT_SIGN_IN_REQUIRED")
        return shift["id"]

    @retry_transaction
    def _record_start(self, phone_id, name, key, latitude, longitude, accuracy):
        with self.database.transaction() as tx:
            if tx.one("SELECT id FROM scanner_phones WHERE id = %s FOR UPDATE", (phone_id,)) is None:
                raise DomainError("SCANNER_ACTIVATION_REQUIRED")
            now = tx.now()
            settings = tx.one("SELECT latitude, longitude, radius_m FROM waiter_shift_settings WHERE id = 1")
            distance = flagged = None
            if settings is not None and settings["latitude"] is not None:
                distance = round(distance_m(latitude, longitude, float(settings["latitude"]), float(settings["longitude"])), 1)
                flagged = distance > settings["radius_m"]
            actor = self._scanner_actor(tx)
            previous = tx.all(
                "SELECT id FROM waiter_shifts WHERE phone_id = %s AND ended_at IS NULL ORDER BY id FOR UPDATE",
                (phone_id,),
            )
            for row in previous:
                tx.execute(
                    "UPDATE waiter_shifts SET ended_at = %s, end_reason = 'REPLACED' WHERE id = %s AND ended_at IS NULL",
                    (now, row["id"]),
                )
                audit(tx, actor, "SHIFT_ENDED", "waiter_shifts", row["id"], after={"end_reason": "REPLACED"})
            shift_id = tx.insert(
                "INSERT INTO waiter_shifts (phone_id, waiter_name, selfie_object_key, latitude, longitude, accuracy_m, "
                "distance_m, flagged, consent_notice_version, consent_at, started_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (phone_id, name, key, round(latitude, 6), round(longitude, 6), round(accuracy, 1), distance, flagged,
                 NOTICE_VERSION, now, now, now + self.length),
            )
            audit(tx, actor, "SHIFT_STARTED", "waiter_shifts", shift_id, after={
                "phone_id": phone_id, "waiter_name": name, "accuracy_m": round(accuracy, 1),
                "distance_m": distance, "flagged": flagged, "consent_notice_version": NOTICE_VERSION,
            })
            row = tx.one("SELECT * FROM waiter_shifts WHERE id = %s", (shift_id,))
        return _summary(row, now)

    def start(self, phone_id, waiter_name, selfie, latitude, longitude, accuracy_m, consent, notice_version):
        phone_id = _id(phone_id, "SCANNER_PHONE_ID")
        if consent is not True or notice_version != NOTICE_VERSION:
            raise DomainError("SHIFT_CONSENT_REQUIRED")
        name = required_text(waiter_name, "WAITER_NAME", 150)
        latitude = _number(latitude, "LATITUDE", -90, 90)
        longitude = _number(longitude, "LONGITUDE", -180, 180)
        accuracy = _number(accuracy_m, "LOCATION_ACCURACY", 0, 1_000_000)
        data = decode_selfie(selfie)
        stored = self.storage.put(data, "image/jpeg", prefix=SELFIE_PREFIX)
        try:
            return self._record_start(phone_id, name, stored.key, latitude, longitude, accuracy)
        except BaseException:
            try:
                self.storage.delete(stored.key)
            except Exception:
                pass
            raise

    @retry_transaction
    def end(self, phone_id):
        phone_id = _id(phone_id, "SCANNER_PHONE_ID")
        with self.database.transaction() as tx:
            now = tx.now()
            row = self._open(tx, phone_id, now, locking=True)
            if row is None:
                return {"ended": False}
            tx.execute(
                "UPDATE waiter_shifts SET ended_at = %s, end_reason = 'SIGNED_OUT' WHERE id = %s AND ended_at IS NULL",
                (now, row["id"]),
            )
            audit(tx, self._scanner_actor(tx), "SHIFT_ENDED", "waiter_shifts", row["id"], after={"end_reason": "SIGNED_OUT"})
        return {"ended": True, "shift_id": row["id"]}

    @retry_transaction
    def admin_end(self, context, shift_id):
        shift_id = _id(shift_id, "SHIFT_ID")
        with self.database.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT * FROM waiter_shifts WHERE id = %s FOR UPDATE", (shift_id,))
            if row is None:
                raise DomainError("SHIFT_NOT_FOUND")
            now = tx.now()
            if row["ended_at"] is not None or row["expires_at"] <= now:
                raise DomainError("SHIFT_ALREADY_ENDED")
            tx.execute(
                "UPDATE waiter_shifts SET ended_at = %s, end_reason = 'ENDED_BY_ADMIN', ended_by_staff_id = %s "
                "WHERE id = %s AND ended_at IS NULL",
                (now, actor.staff_id, shift_id),
            )
            audit(tx, actor.staff_id, "SHIFT_ENDED_BY_ADMIN", "waiter_shifts", shift_id,
                  after={"waiter_name": row["waiter_name"], "phone_id": row["phone_id"]})
        return {"id": shift_id, "ended": True}

    def list(self, context, status="all", limit=50, before=None):
        if status not in {"all", "active", "ended", "flagged"}:
            raise DomainError("INVALID_SHIFT_FILTER")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise DomainError("INVALID_SHIFT_PAGE")
        cursor = parse_cursor(before)
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            now = tx.now()
            filters, params = ["1 = 1"], []
            if status == "active":
                filters.append("w.ended_at IS NULL AND w.expires_at > %s")
                params.append(now)
            elif status == "ended":
                filters.append("(w.ended_at IS NOT NULL OR w.expires_at <= %s)")
                params.append(now)
            elif status == "flagged":
                filters.append("w.flagged = 1")
            condition, values, order = newest_first("w.started_at", "w.id", cursor)
            if condition is not None:
                filters.append(condition)
                params.extend(values)
            rows = tx.all(
                "SELECT w.*, p.label AS phone_label FROM waiter_shifts w JOIN scanner_phones p ON p.id = w.phone_id "
                "WHERE " + " AND ".join(filters) + order,
                (*params, limit + 1),
            )
        return {"items": [_summary(row, now) for row in rows[:limit]], "next_cursor": next_cursor(rows, limit, "started_at")}

    def selfie(self, context, shift_id):
        shift_id = _id(shift_id, "SHIFT_ID")
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT selfie_object_key FROM waiter_shifts WHERE id = %s", (shift_id,))
        if row is None:
            raise DomainError("SHIFT_NOT_FOUND")
        try:
            return self.storage.read(row["selfie_object_key"])
        except DomainError as error:
            if error.code == "PHOTO_NOT_FOUND":
                raise DomainError("SHIFT_SELFIE_EXPIRED") from None
            raise

    def settings(self, context):
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            row = tx.one("SELECT latitude, longitude, radius_m, updated_at FROM waiter_shift_settings WHERE id = 1")
        configured = row is not None and row["latitude"] is not None
        return {
            "configured": configured,
            "latitude": float(row["latitude"]) if configured else None,
            "longitude": float(row["longitude"]) if configured else None,
            "radius_m": row["radius_m"] if configured else None,
            "notice_version": NOTICE_VERSION,
        }

    @retry_transaction
    def update_settings(self, context, latitude, longitude, radius_m):
        if latitude is None and longitude is None and radius_m is None:
            values = (None, None, None)
        else:
            values = (
                round(_number(latitude, "LATITUDE", -90, 90), 6),
                round(_number(longitude, "LONGITUDE", -180, 180), 6),
                int(_number(radius_m, "CANTEEN_RADIUS", 10, 50000)) if type(radius_m) is int else None,
            )
            if values[2] is None:
                raise DomainError("INVALID_CANTEEN_RADIUS")
        with self.database.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            before = tx.one("SELECT latitude, longitude, radius_m FROM waiter_shift_settings WHERE id = 1 FOR UPDATE")
            if before is None:
                raise DomainError("SHIFT_SETTINGS_NOT_CONFIGURED")
            tx.execute(
                "UPDATE waiter_shift_settings SET latitude = %s, longitude = %s, radius_m = %s, "
                "updated_by_staff_id = %s, updated_at = %s WHERE id = 1",
                (*values, actor.staff_id, tx.now()),
            )
            audit(tx, actor.staff_id, "CANTEEN_LOCATION_UPDATED", "waiter_shift_settings", 1,
                  before={key: (None if before[key] is None else float(before[key])) for key in ("latitude", "longitude", "radius_m")},
                  after={"latitude": values[0], "longitude": values[1], "radius_m": values[2]})
        return self.settings(context)
