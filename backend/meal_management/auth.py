import json
from datetime import timedelta

from .errors import DomainError
from .models import Actor, ScanAppContext, ServerContext
from .security import token_digest


def require_actor(tx, context, roles):
    if not isinstance(context, ServerContext):
        raise DomainError("AUTHENTICATION_REQUIRED")
    if type(context.idle_timeout_seconds) is not int or not 60 <= context.idle_timeout_seconds <= 604800:
        raise DomainError("INVALID_SESSION_IDLE_TIMEOUT")
    try:
        digest = token_digest(context.session_token)
    except DomainError:
        raise DomainError("AUTHENTICATION_REQUIRED") from None
    session = tx.one(
        "SELECT s.id, s.staff_id, s.expires_at, s.revoked_at, s.last_seen_at, a.is_active, a.is_scanner "
        "FROM staff_sessions s JOIN staff_accounts a ON a.id = s.staff_id "
        "WHERE s.token_hash = %s FOR UPDATE",
        (digest,),
    )
    now = tx.now()
    if not session or session["revoked_at"] is not None or session["expires_at"] <= now:
        raise DomainError("AUTHENTICATION_REQUIRED")
    if session.get("is_scanner", False):
        raise DomainError("AUTHENTICATION_REQUIRED")
    if session["last_seen_at"] + timedelta(seconds=context.idle_timeout_seconds) <= now:
        raise DomainError("AUTHENTICATION_REQUIRED")
    if not session["is_active"]:
        raise DomainError("STAFF_INACTIVE")
    assigned = tx.all("SELECT role_code FROM staff_account_roles WHERE staff_id = %s FOR SHARE", (session["staff_id"],))
    actual = frozenset(row["role_code"] for row in assigned)
    if roles and not actual.intersection(roles):
        raise DomainError("ROLE_REQUIRED")
    tx.execute("UPDATE staff_sessions SET last_seen_at = %s WHERE id = %s", (now, session["id"]))
    return Actor(session["staff_id"], actual)


def require_scan_actor(tx, context):
    if not isinstance(context, ScanAppContext):
        return require_actor(tx, context, {"ADMIN", "WAITER"})
    if type(context.staff_id) is not int or not 1 <= context.staff_id <= 2**64 - 1:
        raise DomainError("SCAN_APP_UNAVAILABLE")
    profile = tx.one(
        "SELECT p.staff_id, p.is_enabled, a.is_scanner, a.is_active "
        "FROM scan_app_settings p JOIN staff_accounts a ON a.id = p.staff_id "
        "WHERE p.id = 1 AND p.staff_id = %s FOR SHARE",
        (context.staff_id,),
    )
    if (
        not profile or profile["staff_id"] != context.staff_id
        or not profile["is_enabled"] or not profile["is_scanner"] or not profile["is_active"]
    ):
        raise DomainError("SCAN_APP_UNAVAILABLE")
    assigned = tx.all(
        "SELECT role_code FROM staff_account_roles WHERE staff_id = %s FOR SHARE",
        (context.staff_id,),
    )
    actual = frozenset(row["role_code"] for row in assigned)
    if actual != frozenset({"WAITER"}):
        raise DomainError("SCAN_APP_UNAVAILABLE")
    return Actor(context.staff_id, actual)


def audit(tx, actor_id, action, entity_type, entity_id, before=None, after=None):
    return tx.insert(
        "INSERT INTO audit_events (actor_staff_id, action, entity_type, entity_id, before_data, after_data) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            actor_id, action, entity_type, str(entity_id),
            None if before is None else json.dumps(before, default=str, sort_keys=True),
            None if after is None else json.dumps(after, default=str, sort_keys=True),
        ),
    )
