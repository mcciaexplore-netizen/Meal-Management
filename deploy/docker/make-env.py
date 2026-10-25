"""Write .env.docker for the single-server docker compose deployment on EC2 with
Amazon RDS for MySQL, S3 photos and Gmail. Values are never printed.

    python3 deploy/docker/make-env.py ADMIN_HOST SCANNER_HOST \\
        --db-host ENDPOINT --db-name meal_management \\
        --s3-bucket BUCKET --aws-region eu-north-1 \\
        [--gmail-from OLD_ENV_FILE] [--out .env.docker]

Secrets (CSRF and login-rate secrets, QR encryption keys, scanner activation
code, meal_runtime password) are generated on the first run and preserved when
the same --out file is regenerated. Changing QR_ENCRYPTION_KEYS would make every
issued QR code unreadable, so never delete the output file while it is in use.

Values are written unquoted, so the file works both as a compose env_file and
with `docker run --env-file`. Real email sending stays off unless
--email-send-enabled is given.
"""

import argparse
import base64
import getpass
import os
import re
import secrets
import string
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
SAFE_VALUE = re.compile(r"[!%-&(-\[\]-_a-~]*")
PRESERVED = (
    "APP_CSRF_SECRET", "LOGIN_RATE_SECRET", "QR_ENCRYPTION_KEYS",
    "SCANNER_ACTIVATION_SECRET", "DB_PASSWORD", "EMAIL_SENDER", "GMAIL_APP_PASSWORD",
)


def read_env(path):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def app_secret():
    return "base64:" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def fernet_key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def activation_code():
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def database_password():
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(40))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("admin_host")
    parser.add_argument("scanner_host")
    parser.add_argument("--db-host", required=True)
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-name", required=True)
    parser.add_argument("--s3-bucket", required=True)
    parser.add_argument("--aws-region", required=True)
    parser.add_argument("--gmail-from", type=Path, help="Existing env file to copy EMAIL_SENDER and GMAIL_APP_PASSWORD from.")
    parser.add_argument("--email-send-enabled", action="store_true")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / ".env.docker")
    arguments = parser.parse_args()

    admin, scanner = arguments.admin_host.lower(), arguments.scanner_host.lower()
    if not HOST.fullmatch(admin) or not HOST.fullmatch(scanner) or admin == scanner:
        raise SystemExit("Give two different hostnames, e.g. admin.example.com scan.example.com")
    if not HOST.fullmatch(arguments.db_host.lower()):
        raise SystemExit("--db-host must be the RDS endpoint hostname.")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", arguments.db_name):
        raise SystemExit("--db-name must contain only letters, digits and underscores.")
    if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", arguments.aws_region):
        raise SystemExit("--aws-region must look like eu-north-1.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", arguments.s3_bucket):
        raise SystemExit("--s3-bucket is not a valid bucket name.")

    existing = read_env(arguments.out) if arguments.out.is_file() else {}
    preserved = {key: existing[key] for key in PRESERVED if existing.get(key)}
    if arguments.gmail_from:
        source = read_env(arguments.gmail_from)
        for key in ("EMAIL_SENDER", "GMAIL_APP_PASSWORD"):
            if source.get(key):
                preserved.setdefault(key, source[key])
    if not preserved.get("EMAIL_SENDER"):
        preserved["EMAIL_SENDER"] = input("Gmail sender address: ").strip()
    if not preserved.get("GMAIL_APP_PASSWORD"):
        preserved["GMAIL_APP_PASSWORD"] = getpass.getpass("Gmail App Password (input hidden): ")
    preserved["GMAIL_APP_PASSWORD"] = preserved["GMAIL_APP_PASSWORD"].replace(" ", "")
    if not re.fullmatch(r"[A-Za-z]{16}", preserved["GMAIL_APP_PASSWORD"]):
        raise SystemExit("GMAIL_APP_PASSWORD must be a 16-letter Google App Password.")
    if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", preserved["EMAIL_SENDER"]):
        raise SystemExit("EMAIL_SENDER is not a valid email address.")

    values = {
        "APP_ENV": "production",
        "ADMIN_HOST": admin,
        "SCANNER_HOST": scanner,
        "APP_ORIGIN": "https://" + admin,
        "SCANNER_ORIGIN": "https://" + scanner,
        "ALLOWED_HOSTS": admin + "," + scanner,
        "COOKIE_SECURE": "true",
        "APP_CSRF_SECRET": preserved.get("APP_CSRF_SECRET") or app_secret(),
        "LOGIN_RATE_SECRET": preserved.get("LOGIN_RATE_SECRET") or app_secret(),
        "SESSION_IDLE_MINUTES": "30",
        "LOGIN_ATTEMPT_LIMIT": "5",
        "LOGIN_IP_ATTEMPT_LIMIT": "20",
        "LOGIN_WINDOW_SECONDS": "60",
        "SCAN_APP_ENABLED": "true",
        "SCANNER_ACTIVATION_SECRET": preserved.get("SCANNER_ACTIVATION_SECRET") or activation_code(),
        "SCAN_REQUEST_LIMIT": "120",
        "SCAN_IP_LIMIT": "600",
        "SCAN_WINDOW_SECONDS": "60",
        "DB_ENGINE": "mysql",
        "DB_HOST": arguments.db_host.lower(),
        "DB_PORT": str(arguments.db_port),
        "DB_NAME": arguments.db_name,
        "DB_USER": "meal_runtime",
        "DB_PASSWORD": preserved.get("DB_PASSWORD") or database_password(),
        "DB_CONNECT_TIMEOUT": "10",
        "DB_SSL_CA": "/run/db-ca.pem",
        "QR_ENCRYPTION_KEYS": preserved.get("QR_ENCRYPTION_KEYS") or fernet_key(),
        "PHOTO_BACKEND": "s3",
        "AWS_REGION": arguments.aws_region,
        "PHOTO_S3_BUCKET": arguments.s3_bucket,
        "MAX_PHOTO_BYTES": "4000000",
        "EMAIL_BACKEND": "gmail",
        "EMAIL_SENDER": preserved["EMAIL_SENDER"],
        "GMAIL_APP_PASSWORD": preserved["GMAIL_APP_PASSWORD"],
        "EMAIL_SEND_ENABLED": "true" if arguments.email_send_enabled else "false",
        "EMAIL_AUTO_SEND_ENABLED": "false",
    }
    for key, value in values.items():
        if not value or not SAFE_VALUE.fullmatch(value):
            raise SystemExit(key + " is empty or contains quotes, spaces, '#', '$' or backslashes.")

    descriptor = os.open(arguments.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key, value in values.items():
            stream.write(key + "=" + value + "\n")
    os.chmod(arguments.out, 0o600)
    kept = sorted(key for key in PRESERVED if key in existing)
    print("Wrote " + str(arguments.out) + " (" + str(len(values)) + " settings, values not shown).")
    if kept:
        print("Preserved from the existing file: " + ", ".join(kept) + ".")


if __name__ == "__main__":
    main()
