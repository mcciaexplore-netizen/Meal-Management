from datetime import timedelta
from functools import lru_cache

from .auth import audit, require_actor
from .errors import DomainError
from .models import ServerContext
from .security import (
    generate_token,
    hash_password,
    normalize_email,
    required_text,
    token_digest,
    verify_password,
)


@lru_cache(maxsize=1)
def _dummy_password_hash():
    return hash_password(generate_token())


def _role_codes(roles):
    if not isinstance(roles, (list, tuple, set, frozenset)) or not roles:
        raise DomainError("INVALID_ROLES")
    result = {required_text(role, "role", 32).upper() for role in roles}
    if not result <= {"ADMIN", "WAITER", "AUDITOR"}:
        raise DomainError("INVALID_ROLES")
    return sorted(result)


class StaffService:
    def __init__(self, db, session_hours=8):
        if type(session_hours) is not int or not 1 <= session_hours <= 168:
            raise DomainError("INVALID_SESSION_DURATION")
        self.db = db
        self.session_hours = session_hours

    def _lock_administration(self, tx):
        lock = tx.one(
            "SELECT name FROM system_locks WHERE name = %s FOR UPDATE",
            ("bootstrap",),
        )
        if lock is None:
            raise DomainError("DATABASE_NOT_INITIALIZED")

    def _add_roles(self, tx, staff_id, roles):
        for role in roles:
            if tx.one("SELECT code FROM roles WHERE code = %s", (role,)) is None:
                raise DomainError("INVALID_ROLES")
            tx.execute(
                "INSERT INTO staff_account_roles (staff_id, role_code) VALUES (%s, %s)",
                (staff_id, role),
            )

    def _protect_last_admin(self, tx, staff, future_active, future_roles):
        current_roles = {
            row["role_code"]
            for row in tx.all(
                "SELECT role_code FROM staff_account_roles WHERE staff_id = %s",
                (staff["id"],),
            )
        }
        removing_admin = (
            staff["is_active"]
            and "ADMIN" in current_roles
            and (not future_active or "ADMIN" not in future_roles)
        )
        if removing_admin:
            count = tx.one(
                "SELECT COUNT(*) AS total FROM staff_accounts AS s "
                "JOIN staff_account_roles AS r ON r.staff_id = s.id "
                "WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = %s",
                ("ADMIN",),
            )["total"]
            if count <= 1:
                raise DomainError("LAST_ACTIVE_ADMIN")

    def bootstrap_admin(self, display_name, email, password):
        display_name = required_text(display_name, "display_name", 150)
        email = normalize_email(email)
        password_hash = hash_password(password)
        with self.db.transaction() as tx:
            self._lock_administration(tx)
            if tx.one("SELECT id FROM staff_accounts WHERE is_scanner = 0 LIMIT 1") is not None:
                raise DomainError("ADMIN_ALREADY_INITIALIZED")
            now = tx.now()
            staff_id = tx.insert(
                "INSERT INTO staff_accounts "
                "(display_name, email, password_hash, is_active, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (display_name, email, password_hash, True, now, now),
            )
            self._add_roles(tx, staff_id, ["ADMIN"])
            audit(
                tx,
                staff_id,
                "STAFF_BOOTSTRAPPED",
                "staff_accounts",
                staff_id,
                after={"display_name": display_name, "email": email, "roles": ["ADMIN"]},
            )
        return staff_id

    def create_staff(self, context, display_name, email, password, roles):
        display_name = required_text(display_name, "display_name", 150)
        email = normalize_email(email)
        roles = _role_codes(roles)
        password_hash = hash_password(password)
        with self.db.transaction() as tx:
            self._lock_administration(tx)
            actor = require_actor(tx, context, {"ADMIN"})
            if tx.one("SELECT id FROM staff_accounts WHERE email = %s", (email,)):
                raise DomainError("STAFF_EMAIL_EXISTS")
            now = tx.now()
            staff_id = tx.insert(
                "INSERT INTO staff_accounts "
                "(display_name, email, password_hash, is_active, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (display_name, email, password_hash, True, now, now),
            )
            self._add_roles(tx, staff_id, roles)
            audit(
                tx,
                actor.staff_id,
                "STAFF_CREATED",
                "staff_accounts",
                staff_id,
                after={"display_name": display_name, "email": email, "roles": roles},
            )
        return staff_id

    def authenticate(self, email, password):
        try:
            email = normalize_email(email)
        except DomainError:
            raise DomainError("INVALID_CREDENTIALS") from None
        if not isinstance(password, str):
            raise DomainError("INVALID_CREDENTIALS")
        with self.db.transaction() as tx:
            staff = tx.one(
                "SELECT id, password_hash, is_active, is_scanner FROM staff_accounts "
                "WHERE email = %s FOR UPDATE",
                (email,),
            )
            interactive = staff and not staff.get("is_scanner", False)
            encoded = staff["password_hash"] if interactive else _dummy_password_hash()
            valid = verify_password(password, encoded)
            if not interactive or not valid or not staff["is_active"]:
                raise DomainError("INVALID_CREDENTIALS")
            token = generate_token()
            now = tx.now()
            tx.insert(
                "INSERT INTO staff_sessions "
                "(staff_id, token_hash, created_at, expires_at, last_seen_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    staff["id"],
                    token_digest(token),
                    now,
                    now + timedelta(hours=self.session_hours),
                    now,
                ),
            )
        return ServerContext(session_token=token)

    def logout(self, context):
        if not isinstance(context, ServerContext):
            return
        try:
            digest = token_digest(context.session_token)
        except DomainError:
            return
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE staff_sessions SET revoked_at = %s "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (tx.now(), digest),
            )

    def current(self, context):
        with self.db.transaction() as tx:
            actor = require_actor(tx, context, set())
            staff = tx.one(
                "SELECT id AS staff_id, display_name, email FROM staff_accounts WHERE id = %s",
                (actor.staff_id,),
            )
            if staff is None:
                raise DomainError("AUTHENTICATION_REQUIRED")
            result = {**staff, "roles": sorted(actor.roles)}
        return result

    def set_active(self, context, staff_id, is_active):
        if type(staff_id) is not int or staff_id < 1 or type(is_active) is not bool:
            raise DomainError("INVALID_STAFF_UPDATE")
        with self.db.transaction() as tx:
            self._lock_administration(tx)
            actor = require_actor(tx, context, {"ADMIN"})
            staff = tx.one(
                "SELECT id, is_active, is_scanner FROM staff_accounts WHERE id = %s FOR UPDATE",
                (staff_id,),
            )
            if staff is None:
                raise DomainError("STAFF_NOT_FOUND")
            roles = {
                row["role_code"]
                for row in tx.all(
                    "SELECT role_code FROM staff_account_roles WHERE staff_id = %s",
                    (staff_id,),
                )
            }
            self._protect_last_admin(tx, staff, is_active, roles)
            now = tx.now()
            tx.execute(
                "UPDATE staff_accounts SET is_active = %s, updated_at = %s WHERE id = %s",
                (is_active, now, staff_id),
            )
            if not is_active:
                tx.execute(
                    "UPDATE staff_sessions SET revoked_at = %s "
                    "WHERE staff_id = %s AND revoked_at IS NULL",
                    (now, staff_id),
                )
            audit(
                tx,
                actor.staff_id,
                "STAFF_STATUS_CHANGED",
                "staff_accounts",
                staff_id,
                before={"is_active": bool(staff["is_active"])},
                after={"is_active": is_active},
            )

    def set_roles(self, context, staff_id, roles):
        if type(staff_id) is not int or staff_id < 1:
            raise DomainError("INVALID_STAFF_ID")
        roles = _role_codes(roles)
        with self.db.transaction() as tx:
            self._lock_administration(tx)
            actor = require_actor(tx, context, {"ADMIN"})
            staff = tx.one(
                "SELECT id, is_active, is_scanner FROM staff_accounts WHERE id = %s FOR UPDATE",
                (staff_id,),
            )
            if staff is None:
                raise DomainError("STAFF_NOT_FOUND")
            if staff.get("is_scanner", False):
                raise DomainError("SCANNER_ROLES_IMMUTABLE")
            before = [
                row["role_code"]
                for row in tx.all(
                    "SELECT role_code FROM staff_account_roles "
                    "WHERE staff_id = %s ORDER BY role_code",
                    (staff_id,),
                )
            ]
            self._protect_last_admin(tx, staff, staff["is_active"], roles)
            tx.execute("DELETE FROM staff_account_roles WHERE staff_id = %s", (staff_id,))
            self._add_roles(tx, staff_id, roles)
            audit(
                tx,
                actor.staff_id,
                "STAFF_ROLES_CHANGED",
                "staff_accounts",
                staff_id,
                before={"roles": before},
                after={"roles": roles},
            )
