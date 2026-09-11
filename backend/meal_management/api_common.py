from dataclasses import asdict

from .errors import DomainError


def scan_response(result):
    if isinstance(result, dict):
        return result
    if result.code in {"PROCESSING_UNCONFIRMED", "SCAN_RECEIPT_UNCONFIRMED"}:
        raise DomainError(result.code)
    return asdict(result)


def database_readiness(database):
    try:
        with database.transaction() as tx:
            version = tx.one("SELECT VERSION() AS version")["version"]
            applied = tx.all("SELECT version FROM schema_migrations WHERE status = 'APPLIED'")
        if not version.startswith("8.4.") or not {1, 2, 3, 4, 5, 6, 7}.issubset({row["version"] for row in applied}):
            raise DomainError("DATABASE_NOT_READY")
    except Exception:
        raise DomainError("DATABASE_NOT_READY") from None
    return {"status": "ready"}
