import base64
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import datetime, timezone

from .errors import DependencyError, DomainError


def required_text(value, name, maxlength):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maxlength:
        raise DomainError("INVALID_" + name.upper())
    if any(ord(character) < 32 for character in value):
        raise DomainError("INVALID_" + name.upper())
    return value.strip()


def normalize_email(value):
    value = required_text(value, "EMAIL", 254)
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise DomainError("INVALID_EMAIL")
    return value.lower()


def normalize_phone(value, field="PHONE"):
    value = required_text(value, field, 32)
    if not re.fullmatch(r"\+?[0-9 ()\-.]+", value) or not 7 <= sum(character.isdigit() for character in value) <= 15:
        raise DomainError("INVALID_" + field)
    return value


def utc_naive(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("TIMEZONE_REQUIRED")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def generate_token():
    return secrets.token_urlsafe(32)


def token_digest(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise DomainError("INVALID_QR")
    try:
        raw = base64.b64decode(token + "=", altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise DomainError("INVALID_QR") from None
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != token:
        raise DomainError("INVALID_QR")
    return hashlib.sha256(raw).digest()


def payload_digest(payload):
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).digest()


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise DomainError("PASSWORD_LENGTH_INVALID")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
    return "$".join(("scrypt", "32768", "8", "1", base64.b64encode(salt).decode(), base64.b64encode(derived).decode()))


def verify_password(password, encoded):
    if not isinstance(password, str) or len(password) > 1024 or not isinstance(encoded, str):
        return False
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        salt_bytes = base64.b64decode(salt, validate=True)
        expected_bytes = base64.b64decode(expected, validate=True)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt_bytes, n=32768, r=8, p=1, dklen=32, maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


class TokenVault:
    def __init__(self, keys):
        try:
            from cryptography.fernet import Fernet, MultiFernet
        except ImportError:
            raise DependencyError("CRYPTOGRAPHY_NOT_INSTALLED") from None
        try:
            self._cipher = MultiFernet([Fernet(key) for key in keys])
        except (ValueError, TypeError):
            raise DomainError("INVALID_ENCRYPTION_KEY") from None

    def encrypt(self, value):
        return self._cipher.encrypt(value.encode())

    def decrypt(self, value):
        from cryptography.fernet import InvalidToken
        try:
            return self._cipher.decrypt(bytes(value)).decode()
        except (InvalidToken, UnicodeDecodeError, TypeError):
            raise DomainError("ENCRYPTED_PAYLOAD_UNREADABLE") from None


class QrRenderer:
    def render(self, token):
        token_digest(token)
        try:
            import qrcode
            from qrcode.image.svg import SvgPathImage
        except ImportError:
            raise DependencyError("QRCODE_NOT_INSTALLED") from None
        output = io.BytesIO()
        qrcode.make(token, image_factory=SvgPathImage, border=4).save(output)
        return output.getvalue().decode()
