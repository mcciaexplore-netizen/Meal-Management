import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath


ASSETS = {
    "admin": {"app.js", "styles.css", "THIRD_PARTY_NOTICES.txt"},
    "scanner": {"scan-app.js", "styles.css", "scan-only.css", "THIRD_PARTY_NOTICES.txt"},
}


def verify_bundle(root=None, environment=None):
    root = Path(__file__).resolve().parent if root is None else Path(root)
    environment = os.environ if environment is None else environment
    with (root / "bundle-manifest.json").open(encoding="utf-8") as source:
        manifest = json.load(source)
    application = manifest.get("application")
    if manifest.get("format_version") != 1 or application not in ASSETS:
        raise RuntimeError("VERCEL_BUNDLE_MANIFEST_INVALID")
    if environment.get("MEAL_APPLICATION", application) != application:
        raise RuntimeError("VERCEL_BUNDLE_APPLICATION_MISMATCH")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("VERCEL_BUNDLE_MANIFEST_INVALID")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or relative.as_posix() != name or ".." in relative.parts or "\\" in name:
            raise RuntimeError("VERCEL_BUNDLE_UNSAFE_PATH")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError("VERCEL_BUNDLE_MANIFEST_INVALID")
        path = root / name
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise RuntimeError("VERCEL_BUNDLE_UNSAFE_PATH")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("VERCEL_BUNDLE_UNSAFE_PATH")
            if hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise RuntimeError("VERCEL_BUNDLE_CHECKSUM_MISMATCH")
    expected_public = {"assets/" + name for name in ASSETS[application]}
    actual_public = {path.relative_to(root / "public").as_posix() for path in (root / "public").rglob("*") if path.is_file() or path.is_symlink()}
    if actual_public != expected_public:
        raise RuntimeError("VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH")
    frontend = root / "frontend" / "dist"
    if {path.name for path in frontend.iterdir()} != {application}:
        raise RuntimeError("VERCEL_BUNDLE_APPLICATION_MISMATCH")
    if {path.name for path in (frontend / application).iterdir()} != ASSETS[application] | {"index.html"}:
        raise RuntimeError("VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH")
    return application


if __name__ == "__main__":
    try:
        role = verify_bundle()
    except Exception:
        raise SystemExit("Vercel package verification failed. Rebuild the selected application package.") from None
    print("Verified " + role + " application package.")
