"""Write .env.docker for docker-compose from the reviewed Vercel production
environment (same database account, QR keys, CSRF/login secrets, Blob store and
Gmail settings), re-pointed at new hostnames. Values are never printed.

    python3 deploy/docker/make-env.py ADMIN_HOST SCANNER_HOST [--out .env.docker]
"""

import argparse
import getpass
import json
import os
import re
import secrets
import string
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "var" / "private" / "vercel" / "environment-20260910" / "admin.env.json"
DROPPED = {"MEAL_APPLICATION", "SCANNER_ALLOWED_CIDRS"}
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def quoted(value):
    # docker compose env_file: double-quoted values keep newlines as \n escapes,
    # and "$" must be doubled or compose treats it as a variable.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("$", "$$") + '"'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("admin_host")
    parser.add_argument("scanner_host")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / ".env.docker")
    arguments = parser.parse_args()
    admin, scanner = arguments.admin_host.lower(), arguments.scanner_host.lower()
    if not HOST.fullmatch(admin) or not HOST.fullmatch(scanner) or admin == scanner:
        raise SystemExit("Give two different hostnames, e.g. admin.example.com scan.example.com")

    values = {entry["key"]: entry["value"] for entry in json.loads(arguments.source.read_text())}
    for key in DROPPED:
        values.pop(key, None)
    values.update({
        "ADMIN_HOST": admin,
        "SCANNER_HOST": scanner,
        "APP_ORIGIN": "https://" + admin,
        "SCANNER_ORIGIN": "https://" + scanner,
        "ALLOWED_HOSTS": admin + "," + scanner,
        "SCAN_APP_ENABLED": "true",
    })
    if not values.get("BLOB_READ_WRITE_TOKEN"):
        # Vercel injects this token at deploy time, so it is not in the saved
        # payload. Copy it from Vercel: Storage > the Blob store > .env.local tab.
        values["BLOB_READ_WRITE_TOKEN"] = os.environ.get("BLOB_READ_WRITE_TOKEN") or getpass.getpass(
            "Paste BLOB_READ_WRITE_TOKEN (input hidden): "
        ).strip()
        if not values["BLOB_READ_WRITE_TOKEN"]:
            raise SystemExit("BLOB_READ_WRITE_TOKEN is required for photo storage.")
    existing = {}
    if arguments.out.is_file():
        for line in arguments.out.read_text().splitlines():
            if line.startswith("SCANNER_ACTIVATION_SECRET="):
                existing["SCANNER_ACTIVATION_SECRET"] = json.loads(line.split("=", 1)[1])
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    values["SCANNER_ACTIVATION_SECRET"] = existing.get(
        "SCANNER_ACTIVATION_SECRET", "".join(secrets.choice(alphabet) for _ in range(16))
    )

    descriptor = os.open(arguments.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        for key in sorted(values):
            stream.write(key + "=" + quoted(values[key]) + "\n")
    os.chmod(arguments.out, 0o600)
    print("Wrote " + str(arguments.out) + " (" + str(len(values)) + " settings, values not shown).")


if __name__ == "__main__":
    main()
