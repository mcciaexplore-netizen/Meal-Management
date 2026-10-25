import calendar
import hashlib
import hmac
import re
import secrets
from datetime import date, datetime, time, timedelta, timezone

from .auth import audit, require_actor
from .database import retry_transaction, savepoint
from .errors import DomainError
from .security import required_text


CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_LIFETIME = timedelta(hours=24)
SECRET_ACTIVATION_LIFETIME = timedelta(days=30)
MAX_ACTIVE_MONTHS = 6
IST = timezone(timedelta(hours=5, minutes=30), "IST")
LEGACY_LABEL = "Activated before codes"
SECRET_LABEL = "Emergency code"
_CODE_PATTERN = re.compile("[" + CODE_ALPHABET + "]{" + str(CODE_LENGTH) + "}")


def generate_code():
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(value):
    if not isinstance(value, str) or len(value) > 32:
        return None
    candidate = re.sub(r"[\s-]", "", value).upper()
    return candidate if _CODE_PATTERN.fullmatch(candidate) else None


def display_code(code):
    return code[:4] + "-" + code[4:]


def ist_today(now):
    return now.replace(tzinfo=timezone.utc).astimezone(IST).date()


def end_of_ist_day(day):
    return datetime.combine(day + timedelta(days=1), time(), IST).astimezone(timezone.utc).replace(tzinfo=None)


def ist_day_of(active_until):
    if active_until is None:
        return None
    return (active_until.replace(tzinfo=timezone.utc) - timedelta(microseconds=1)).astimezone(IST).date()


def add_months(day, months):
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def latest_active_day(now):
    return add_months(ist_today(now), MAX_ACTIVE_MONTHS)


def active_until_for(day, now):
    if type(day) is not date:
        raise DomainError("INVALID_ACTIVE_UNTIL")
    if not ist_today(now) <= day <= latest_active_day(now):
        raise DomainError("ACTIVE_UNTIL_OUT_OF_RANGE")
    return end_of_ist_day(day)


def _phone_id(value):
    if type(value) is not int or not 1 <= value <= 2**64 - 1:
        raise DomainError("INVALID_SCANNER_PHONE_ID")
    return value


def _browser_hash(value):
    if not isinstance(value, bytes) or len(value) != 32:
        raise DomainError("SCANNER_BROWSER_REQUIRED")
    return value


def _summary(row):
    return {
        "id": row["id"],
        "label": row["label"],
        "source": row["source"],
        "created_at": row["created_at"],
        "code_expires_at": row["code_expires_at"],
        "activated_at": row["activated_at"],
        "active_until": row["active_until"],
        "active_until_date": None if row["active_until"] is None else ist_day_of(row["active_until"]).isoformat(),
    }


class ScannerPhoneService:
    def __init__(self, database, secret):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise DomainError("INVALID_SCANNER_CODE_SECRET")
        self.database = database
        self.secret = secret

    def code_hash(self, code):
        return hmac.new(self.secret, b"scanner-access-code:" + code.encode(), hashlib.sha256).digest()

    def _scanner_actor(self, tx):
        row = tx.one("SELECT staff_id FROM scan_app_settings WHERE id = 1")
        return None if row is None else row["staff_id"]

    @retry_transaction
    def create_code(self, context, label, active_until):
        label = required_text(label, "SCANNER_LABEL", 100)
        with self.database.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            now = tx.now()
            until = active_until_for(active_until, now)
            expires = now + CODE_LIFETIME
            for _ in range(5):
                code = generate_code()
                if tx.one("SELECT id FROM scanner_phones WHERE code_hash = %s", (self.code_hash(code),)) is None:
                    break
            else:
                raise DomainError("SCANNER_CODE_UNAVAILABLE")
            phone_id = tx.insert(
                "INSERT INTO scanner_phones (label, source, code_hash, code_expires_at, active_until, "
                "created_by_staff_id, created_at) VALUES (%s, 'CODE', %s, %s, %s, %s, %s)",
                (label, self.code_hash(code), expires, until, actor.staff_id, now),
            )
            audit(tx, actor.staff_id, "SCANNER_CODE_CREATED", "scanner_phones", phone_id, after={
                "label": label, "code_expires_at": expires, "active_until": until,
            })
        return {
            "id": phone_id, "label": label, "code": display_code(code), "code_expires_at": expires,
            "active_until": until, "active_until_date": active_until.isoformat(),
        }

    def list(self, context):
        with self.database.transaction() as tx:
            require_actor(tx, context, {"ADMIN"})
            now = tx.now()
            phones = tx.all(
                "SELECT id, label, source, created_at, code_expires_at, activated_at, active_until "
                "FROM scanner_phones WHERE revoked_at IS NULL AND activated_at IS NOT NULL "
                "AND (active_until IS NULL OR active_until > %s) ORDER BY activated_at DESC, id DESC",
                (now,),
            )
            codes = tx.all(
                "SELECT id, label, source, created_at, code_expires_at, activated_at, active_until "
                "FROM scanner_phones WHERE revoked_at IS NULL AND activated_at IS NULL "
                "AND code_expires_at > %s AND active_until > %s ORDER BY created_at DESC, id DESC",
                (now, now),
            )
        return {
            "phones": [_summary(row) for row in phones],
            "codes": [_summary(row) for row in codes],
            "latest_active_date": latest_active_day(now).isoformat(),
            "today": ist_today(now).isoformat(),
        }

    @retry_transaction
    def revoke(self, context, phone_id):
        phone_id = _phone_id(phone_id)
        with self.database.transaction() as tx:
            actor = require_actor(tx, context, {"ADMIN"})
            row = tx.one(
                "SELECT id, label, source, activated_at, revoked_at FROM scanner_phones WHERE id = %s FOR UPDATE",
                (phone_id,),
            )
            if row is None:
                raise DomainError("SCANNER_PHONE_NOT_FOUND")
            if row["revoked_at"] is not None:
                raise DomainError("SCANNER_PHONE_ALREADY_REVOKED")
            now = tx.now()
            tx.execute(
                "UPDATE scanner_phones SET revoked_at = %s, revoked_by_staff_id = %s WHERE id = %s AND revoked_at IS NULL",
                (now, actor.staff_id, phone_id),
            )
            action = "SCANNER_PHONE_REVOKED" if row["activated_at"] is not None else "SCANNER_CODE_CANCELLED"
            audit(tx, actor.staff_id, action, "scanner_phones", phone_id, after={
                "label": row["label"], "source": row["source"], "revoked_at": now,
            })
        return {"id": phone_id, "revoked": True}

    @retry_transaction
    def activate_code(self, code, browser_hash):
        browser_hash = _browser_hash(browser_hash)
        normalized = normalize_code(code)
        if normalized is None:
            raise DomainError("SCANNER_ACTIVATION_INVALID")
        with self.database.transaction() as tx:
            row = tx.one(
                "SELECT id, label, source, code_expires_at, active_until, activated_at, revoked_at "
                "FROM scanner_phones WHERE code_hash = %s FOR UPDATE",
                (self.code_hash(normalized),),
            )
            now = tx.now()
            if (
                row is None or row["source"] != "CODE" or row["activated_at"] is not None
                or row["revoked_at"] is not None or row["code_expires_at"] <= now or row["active_until"] <= now
            ):
                raise DomainError("SCANNER_ACTIVATION_INVALID")
            tx.execute(
                "UPDATE scanner_phones SET browser_hash = %s, activated_at = %s "
                "WHERE id = %s AND activated_at IS NULL AND revoked_at IS NULL",
                (browser_hash, now, row["id"]),
            )
            audit(tx, self._scanner_actor(tx), "SCANNER_PHONE_ACTIVATED", "scanner_phones", row["id"], after={
                "label": row["label"], "source": "CODE", "activated_at": now, "active_until": row["active_until"],
            })
        return {"id": row["id"], "active_until": row["active_until"]}

    @retry_transaction
    def register_secret_activation(self, browser_hash):
        browser_hash = _browser_hash(browser_hash)
        with self.database.transaction() as tx:
            now = tx.now()
            until = now + SECRET_ACTIVATION_LIFETIME
            phone_id = tx.insert(
                "INSERT INTO scanner_phones (label, source, active_until, browser_hash, activated_at, created_at) "
                "VALUES (%s, 'SECRET', %s, %s, %s, %s)",
                (SECRET_LABEL, until, browser_hash, now, now),
            )
            audit(tx, self._scanner_actor(tx), "SCANNER_PHONE_ACTIVATED", "scanner_phones", phone_id, after={
                "label": SECRET_LABEL, "source": "SECRET", "activated_at": now, "active_until": until,
            })
        return {"id": phone_id, "active_until": until}

    @retry_transaction
    def authorize(self, browser_hash, adopt_legacy=False):
        browser_hash = _browser_hash(browser_hash)
        with self.database.transaction() as tx:
            now = tx.now()
            row = tx.one(
                "SELECT id, active_until, revoked_at FROM scanner_phones WHERE browser_hash = %s",
                (browser_hash,),
            )
            if row is None:
                if not adopt_legacy:
                    raise DomainError("SCANNER_ACTIVATION_REQUIRED")
                try:
                    with savepoint(tx):
                        phone_id = tx.insert(
                            "INSERT INTO scanner_phones (label, source, browser_hash, activated_at, created_at) "
                            "VALUES (%s, 'LEGACY', %s, %s, %s)",
                            (LEGACY_LABEL, browser_hash, now, now),
                        )
                except Exception as error:
                    if getattr(error, "errno", None) != 1062:
                        raise
                    row = tx.one(
                        "SELECT id, active_until, revoked_at FROM scanner_phones WHERE browser_hash = %s FOR SHARE",
                        (browser_hash,),
                    )
                    if row is None:
                        raise DomainError("SCANNER_ACTIVATION_REQUIRED") from None
                else:
                    audit(tx, self._scanner_actor(tx), "SCANNER_PHONE_REGISTERED", "scanner_phones", phone_id, after={
                        "label": LEGACY_LABEL, "source": "LEGACY", "activated_at": now,
                    })
                    return {"id": phone_id, "active_until": None}
            if row["revoked_at"] is not None or (row["active_until"] is not None and row["active_until"] <= now):
                raise DomainError("SCANNER_ACTIVATION_REQUIRED")
        return {"id": row["id"], "active_until": row["active_until"]}
