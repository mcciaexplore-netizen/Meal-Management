import base64
import os
import re
from dataclasses import dataclass, field

from .errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    qr_encryption_keys: tuple[str, ...] = field(repr=False)
    db_connect_timeout: int = 10
    db_ssl_ca: str | None = None

    @classmethod
    def from_env(cls, mapping=None):
        env = os.environ if mapping is None else mapping
        required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "QR_ENCRYPTION_KEYS")
        missing = tuple(key for key in required if not isinstance(env.get(key), str) or not env[key].strip())
        if missing:
            error = ConfigurationError("MISSING_CONFIGURATION")
            error.missing = missing
            raise error
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", env["DB_NAME"]):
            raise ConfigurationError("INVALID_DATABASE_NAME")
        try:
            port = int(env.get("DB_PORT", "3306"))
            timeout = int(env.get("DB_CONNECT_TIMEOUT", "10"))
        except (ValueError, TypeError):
            raise ConfigurationError("INVALID_DATABASE_CONFIGURATION") from None
        if not 1 <= port <= 65535 or not 1 <= timeout <= 120:
            raise ConfigurationError("INVALID_DATABASE_CONFIGURATION")
        keys = tuple(key.strip() for key in env["QR_ENCRYPTION_KEYS"].split(","))
        try:
            if any(len(key) != 44 or len(base64.b64decode(key, altchars=b"-_", validate=True)) != 32 for key in keys):
                raise ValueError
        except (ValueError, TypeError):
            raise ConfigurationError("INVALID_ENCRYPTION_KEY") from None
        return cls(
            db_host=env["DB_HOST"].strip(),
            db_port=port,
            db_name=env["DB_NAME"],
            db_user=env["DB_USER"].strip(),
            db_password=env["DB_PASSWORD"],
            qr_encryption_keys=keys,
            db_connect_timeout=timeout,
            db_ssl_ca=env.get("DB_SSL_CA") or None,
        )
