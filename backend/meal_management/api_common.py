from dataclasses import asdict

from .database import is_postgres
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
            if is_postgres(tx):
                version = tx.one("SELECT current_setting('server_version_num')::integer AS version")["version"]
                supported = version >= 150000
                required = {1}
            else:
                version = tx.one("SELECT VERSION() AS version")["version"]
                supported = version.startswith("8.4.")
                required = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}
            applied = tx.all("SELECT version FROM schema_migrations WHERE status = 'APPLIED'")
        if not supported or not required.issubset({row["version"] for row in applied}):
            raise DomainError("DATABASE_NOT_READY")
    except Exception:
        raise DomainError("DATABASE_NOT_READY") from None
    return {"status": "ready"}
