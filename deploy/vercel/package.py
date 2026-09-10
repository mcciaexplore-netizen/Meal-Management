import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_MODULES = (
    "__init__", "accounts", "admin_api", "api", "api_common", "api_schemas", "application", "auth",
    "catalog", "config", "database", "delivery", "development_seed", "email_actions", "email_dispatcher",
    "email_lifecycle", "email_queue", "email_worker", "employees", "errors", "http_security", "meals",
    "models", "qr", "queries", "reports", "runtime", "scan_app", "scan_receipts", "scanner_access",
    "scanner_api", "security", "storage", "vercel_runtime",
)
ASSETS = {
    "admin": ("app.js", "styles.css", "THIRD_PARTY_NOTICES.txt"),
    "scanner": ("scan-app.js", "styles.css", "scan-only.css", "THIRD_PARTY_NOTICES.txt"),
}
CONTENT_SECURITY_POLICY = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'; frame-src 'self' blob:"


class PackagingError(Exception):
    pass


def _regular_bytes(path):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise PackagingError("VERCEL_PACKAGE_REFUSES_SYMLINKS")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PackagingError("VERCEL_PACKAGE_REQUIRES_REGULAR_FILES")
        return source.read()


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(data)


def _runtime_sources(project_root):
    sources = {}
    for name in RUNTIME_MODULES:
        content = _regular_bytes(project_root / "backend" / "meal_management" / (name + ".py"))
        for node in ast.walk(ast.parse(content)):
            if isinstance(node, ast.ImportFrom) and node.level:
                names = [node.module.split(".")[0]] if node.module else [item.name for item in node.names]
                if node.level != 1 or any(module not in RUNTIME_MODULES for module in names):
                    raise PackagingError("VERCEL_PACKAGE_RUNTIME_IMPORT_NOT_ALLOWLISTED")
        sources["backend/meal_management/" + name + ".py"] = content
    return sources


def _build_frontend(project_root, output):
    executable = shutil.which("node")
    if executable is None:
        raise PackagingError("VERCEL_PACKAGE_NODE_REQUIRED")
    script = "import { buildApplications } from " + json.dumps((project_root / "frontend" / "build.mjs").as_uri()) + "; await buildApplications(process.argv[1]);"
    result = subprocess.run(
        [executable, "--input-type=module", "-e", script, str(output)],
        cwd=project_root, env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=120,
    )
    if result.returncode:
        raise PackagingError("VERCEL_PACKAGE_FRONTEND_BUILD_FAILED")


def _entry(application):
    return (
        "import os\nimport sys\nfrom pathlib import Path\n\n\n"
        "APPLICATION = " + repr(application) + "\n"
        "if os.environ.get(\"MEAL_APPLICATION\", APPLICATION) != APPLICATION:\n"
        "    raise RuntimeError(\"VERCEL_APPLICATION_MISMATCH\")\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent / \"backend\"))\n\n"
        "from meal_management.vercel_runtime import create_vercel_app\n\n\n"
        "app = create_vercel_app(APPLICATION)\n"
    ).encode("utf-8")


def _configuration():
    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "SAMEORIGIN",
        "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "Strict-Transport-Security": "max-age=31536000",
    }
    return {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "framework": "fastapi",
        "buildCommand": "python verify_bundle.py",
        "functions": {"app.py": {"maxDuration": 300, "excludeFiles": "public/**"}},
        "headers": [{"source": "/assets/(.*)", "headers": [{"key": key, "value": value} for key, value in headers.items()]}],
    }


def _requirements(project_root):
    project = tomllib.loads(_regular_bytes(project_root / "pyproject.toml").decode("utf-8"))["project"]
    values = project["dependencies"] + project.get("optional-dependencies", {}).get("vercel", [])
    if not isinstance(values, list) or not values:
        raise PackagingError("VERCEL_PACKAGE_DEPENDENCIES_REQUIRED")
    for value in values:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~ -]*", value):
            raise PackagingError("VERCEL_PACKAGE_UNSUPPORTED_DEPENDENCY")
    return ("\n".join(dict.fromkeys(values)) + "\n").encode("utf-8")


def _ignore_file(files):
    allowed = set(files) | {"bundle-manifest.json", ".vercelignore"}
    directories = {parent.as_posix() for name in allowed for parent in Path(name).parents if parent != Path(".")}
    lines = ["*"] + ["!/" + name for name in sorted(directories, key=lambda name: (name.count("/"), name))]
    lines.extend("!/" + name for name in sorted(allowed))
    return ("\n".join(lines) + "\n").encode("utf-8")


def package_application(application, *, project_root=PROJECT_ROOT, output_directory=None):
    if application not in ASSETS:
        raise PackagingError("VERCEL_PACKAGE_INVALID_APPLICATION")
    project_root = Path(project_root).absolute()
    output = Path(output_directory).absolute() if output_directory is not None else project_root / "build" / "vercel" / application
    if output.exists():
        raise PackagingError("VERCEL_PACKAGE_OUTPUT_ALREADY_EXISTS")
    if any(item.is_symlink() for item in (output, *output.parents)):
        raise PackagingError("VERCEL_PACKAGE_REFUSES_SYMLINKS")
    source_files = _runtime_sources(project_root)
    source_files["requirements.txt"] = _requirements(project_root)
    source_files[".python-version"] = b"3.12\n"
    source_files["app.py"] = _entry(application)
    source_files["verify_bundle.py"] = _regular_bytes(Path(__file__).resolve().with_name("verify_bundle.py"))
    source_files["vercel.json"] = (json.dumps(_configuration(), indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meal-vercel-build-", dir=output.parent) as temporary:
        temporary = Path(temporary)
        built = temporary / "frontend"
        _build_frontend(project_root, built)
        for name in (*ASSETS[application], "index.html"):
            content = _regular_bytes(built / application / name)
            if not content:
                raise PackagingError("VERCEL_PACKAGE_FRONTEND_ASSET_EMPTY")
            source_files["frontend/dist/" + application + "/" + name] = content
            if name != "index.html":
                source_files["public/assets/" + name] = content
        stage = temporary / "package"
        stage.mkdir(mode=0o700)
        for name, content in source_files.items():
            _write(stage / name, content)
        manifest = {
            "format_version": 1, "application": application,
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(source_files.items())},
            "json_files": {"vercel.json": hashlib.sha256(json.dumps(
                json.loads(source_files["vercel.json"]), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")).hexdigest()},
        }
        _write(stage / "bundle-manifest.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
        _write(stage / ".vercelignore", _ignore_file(source_files))
        if output.exists():
            raise PackagingError("VERCEL_PACKAGE_OUTPUT_ALREADY_EXISTS")
        stage.rename(output)
    return {"application": application, "directory": str(output), "file_count": len(source_files) + 2,
            "bytes": sum(len(content) for content in source_files.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="package-vercel")
    parser.add_argument("application", choices=tuple(ASSETS))
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = package_application(arguments.application, output_directory=arguments.output)
    except PackagingError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Vercel packaging failed. Check the selected source files and installed frontend build dependencies.", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    print("Prepared locally. No files were uploaded and no deployment was created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
