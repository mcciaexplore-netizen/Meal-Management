import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath


ASSETS = {
    "admin": {"app.js", "styles.css", "THIRD_PARTY_NOTICES.txt"},
    "scanner": {"scan-app.js", "styles.css", "scan-only.css", "THIRD_PARTY_NOTICES.txt"},
}
ERROR_CODES = frozenset({
    "VERCEL_BUNDLE_MANIFEST_INVALID", "VERCEL_BUNDLE_MANIFEST_READ_FAILED",
    "VERCEL_BUNDLE_APPLICATION_MISMATCH", "VERCEL_BUNDLE_UNSAFE_PATH",
    "VERCEL_BUNDLE_UNSAFE_PATH_SYMLINK", "VERCEL_BUNDLE_LINK_COUNT_UNSUPPORTED",
    "VERCEL_BUNDLE_CHECKSUM_MISMATCH", "VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH",
    "VERCEL_BUNDLE_PAYLOAD_MISSING", "VERCEL_BUNDLE_UNEXPECTED_ERROR",
    "VERCEL_BUNDLE_CONFIGURATION_MISMATCH",
})
STAGES = frozenset({"MANIFEST", "FILE", "STATIC", "FRONTEND", "UNKNOWN"})
ERROR_TYPES = frozenset({
    "VerificationError", "FileNotFoundError", "PermissionError", "OSError", "IsADirectoryError",
    "NotADirectoryError", "UnicodeDecodeError", "JSONDecodeError", "TypeError", "ValueError",
    "AttributeError", "RuntimeError", "Exception",
})


class VerificationError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("INVALID_JSON")
        value[key] = item
    return value


def _reject_json_constant(value):
    raise ValueError("INVALID_JSON")


def canonical_json_sha256(data):
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def verification_diagnostic(error):
    attributes = vars(error)
    code = attributes.get("code")
    stage = attributes.get("stage")
    kind = attributes.get("error_type", type(error).__name__)
    diagnostic = {
        "code": code if isinstance(code, str) and code in ERROR_CODES else "VERCEL_BUNDLE_UNEXPECTED_ERROR",
        "stage": stage if isinstance(stage, str) and stage in STAGES else "UNKNOWN",
        "error_type": kind if isinstance(kind, str) and kind in ERROR_TYPES else "Exception",
    }
    entry = attributes.get("file_entry")
    if type(entry) is int and 1 <= entry <= 1000000:
        diagnostic["file_entry"] = entry
    number = attributes.get("errno")
    if type(number) is int and 1 <= number <= 4095:
        diagnostic["errno"] = number
    return diagnostic


def verify_bundle(root=None, environment=None):
    context = {"stage": "MANIFEST", "file_entry": None}
    try:
        return _verify_bundle(root, environment, context)
    except Exception as error:
        if isinstance(error, VerificationError):
            failure = error
        else:
            code = "VERCEL_BUNDLE_UNEXPECTED_ERROR"
            if context["stage"] == "MANIFEST":
                code = "VERCEL_BUNDLE_MANIFEST_READ_FAILED"
            elif context["stage"] == "FILE" and isinstance(error, FileNotFoundError):
                code = "VERCEL_BUNDLE_PAYLOAD_MISSING"
            failure = VerificationError(code)
        failure.stage = context["stage"]
        failure.file_entry = context["file_entry"]
        failure.error_type = type(error).__name__
        if isinstance(error, OSError):
            failure.errno = error.errno
        raise failure from None


def _verify_bundle(root, environment, context):
    root = Path(__file__).resolve().parent if root is None else Path(root)
    environment = os.environ if environment is None else environment
    with (root / "bundle-manifest.json").open(encoding="utf-8") as source:
        manifest = json.load(source, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    if not isinstance(manifest, dict):
        raise VerificationError("VERCEL_BUNDLE_MANIFEST_INVALID")
    application = manifest.get("application")
    if manifest.get("format_version") != 1 or application not in ASSETS:
        raise VerificationError("VERCEL_BUNDLE_MANIFEST_INVALID")
    if environment.get("MEAL_APPLICATION", application) != application:
        raise VerificationError("VERCEL_BUNDLE_APPLICATION_MISMATCH")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise VerificationError("VERCEL_BUNDLE_MANIFEST_INVALID")
    json_files = manifest.get("json_files", {})
    if "json_files" in manifest and (
        not isinstance(json_files, dict) or set(json_files) != {"vercel.json"} or "vercel.json" not in files
        or not isinstance(json_files["vercel.json"], str) or not re.fullmatch(r"[0-9a-f]{64}", json_files["vercel.json"])
    ):
        raise VerificationError("VERCEL_BUNDLE_MANIFEST_INVALID")
    context["stage"] = "FILE"
    for entry, (name, expected) in enumerate(files.items(), start=1):
        context["file_entry"] = entry
        relative = PurePosixPath(name)
        if relative.is_absolute() or relative.as_posix() != name or ".." in relative.parts or "\\" in name:
            raise VerificationError("VERCEL_BUNDLE_UNSAFE_PATH")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise VerificationError("VERCEL_BUNDLE_MANIFEST_INVALID")
        path = root / name
        if any(item.is_symlink() for item in (path, *path.parents)):
            raise VerificationError("VERCEL_BUNDLE_UNSAFE_PATH_SYMLINK")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise VerificationError("VERCEL_BUNDLE_UNSAFE_PATH")
            if metadata.st_nlink != 1:
                raise VerificationError("VERCEL_BUNDLE_LINK_COUNT_UNSUPPORTED")
            if name in json_files:
                try:
                    actual = canonical_json_sha256(source.read())
                except (ValueError, UnicodeError, TypeError, OverflowError):
                    raise VerificationError("VERCEL_BUNDLE_CONFIGURATION_MISMATCH") from None
                if actual != json_files[name]:
                    raise VerificationError("VERCEL_BUNDLE_CONFIGURATION_MISMATCH")
            elif hashlib.file_digest(source, "sha256").hexdigest() != expected:
                raise VerificationError("VERCEL_BUNDLE_CHECKSUM_MISMATCH")
    context.update(stage="STATIC", file_entry=None)
    expected_public = {"assets/" + name for name in ASSETS[application]}
    actual_public = {path.relative_to(root / "public").as_posix() for path in (root / "public").rglob("*") if path.is_file() or path.is_symlink()}
    if actual_public != expected_public:
        raise VerificationError("VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH")
    context["stage"] = "FRONTEND"
    frontend = root / "frontend" / "dist"
    if {path.name for path in frontend.iterdir()} != {application}:
        raise VerificationError("VERCEL_BUNDLE_APPLICATION_MISMATCH")
    if {path.name for path in (frontend / application).iterdir()} != ASSETS[application] | {"index.html"}:
        raise VerificationError("VERCEL_BUNDLE_STATIC_ASSETS_MISMATCH")
    return application


def main():
    try:
        role = verify_bundle()
    except Exception as error:
        print("Vercel package verification failed. Share this diagnostic: " + json.dumps(verification_diagnostic(error), sort_keys=True), file=sys.stderr)
        return 1
    print("Verified " + role + " application package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
