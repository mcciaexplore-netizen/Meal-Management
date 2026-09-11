import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("meal_vercel_git_packaging", Path(__file__).with_name("package.py"))
packaging = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packaging)
MANIFEST = "git-build-manifest.json"
DIRECTORIES = ("backend", "frontend", "public")
ERROR_CODES = frozenset({
    "VERCEL_GIT_INVALID_APPLICATION", "VERCEL_GIT_APPLICATION_MISMATCH", "VERCEL_GIT_UNSAFE_PATH", "VERCEL_GIT_SCAFFOLD_MISMATCH",
    "VERCEL_GIT_DEPENDENCIES_MISMATCH", "VERCEL_GIT_EXISTING_OUTPUT_INVALID",
    "VERCEL_GIT_UNEXPECTED_OUTPUT", "VERCEL_GIT_SOURCE_OR_FRONTEND_INVALID",
    "VERCEL_GIT_BUILD_IN_PROGRESS", "VERCEL_GIT_BUILD_FAILED",
})


class GitBuildError(Exception):
    pass


def _directory(path):
    if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_dir():
        raise GitBuildError("VERCEL_GIT_UNSAFE_PATH")


def _bytes(path):
    try:
        return packaging._regular_bytes(path)
    except (OSError, packaging.PackagingError):
        raise GitBuildError("VERCEL_GIT_UNSAFE_PATH") from None


def _scaffold(root, project_root, application):
    _directory(root)
    if _bytes(root / "app.py") != packaging._entry(application) or _bytes(root / ".python-version").strip() != b"3.12":
        raise GitBuildError("VERCEL_GIT_SCAFFOLD_MISMATCH")
    configuration = tomllib.loads(_bytes(root / "pyproject.toml").decode("utf-8"))
    dependencies = configuration.get("project", {}).get("dependencies")
    expected = packaging._requirements(project_root).decode("utf-8").splitlines()
    if dependencies != expected:
        raise GitBuildError("VERCEL_GIT_DEPENDENCIES_MISMATCH")
    tools = configuration.get("tool", {})
    vercel = tools.get("vercel", {})
    if (
        vercel.get("entrypoint") != "app:app"
        or vercel.get("fastapi", {}).get("static", {}).get("cdn") is not False
        or tools.get("uv", {}).get("package") is not False
    ):
        raise GitBuildError("VERCEL_GIT_SCAFFOLD_MISMATCH")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID")
        value[key] = item
    return value


def _generated_files(root, names):
    directories = {parent.as_posix() for name in names for parent in Path(name).parents if parent != Path(".")}
    found = set()
    pending = [root / name for name in DIRECTORIES]
    while pending:
        path = pending.pop()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        name = path.relative_to(root).as_posix()
        if stat.S_ISLNK(metadata.st_mode):
            raise GitBuildError("VERCEL_GIT_UNSAFE_PATH")
        if stat.S_ISDIR(metadata.st_mode):
            if name not in directories:
                raise GitBuildError("VERCEL_GIT_UNEXPECTED_OUTPUT")
            pending.extend(path.iterdir())
        elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            if name not in names:
                raise GitBuildError("VERCEL_GIT_UNEXPECTED_OUTPUT")
            found.add(name)
        else:
            raise GitBuildError("VERCEL_GIT_UNSAFE_PATH")
    return found


def _existing(root, application, names):
    found = _generated_files(root, names)
    accepted_names = [names]
    if application == "scanner":
        installation_names = {
            prefix + name
            for prefix in ("frontend/dist/scanner/", "public/assets/")
            for name in packaging.SCANNER_INSTALL_ASSETS
        }
        accepted_names.append(names - installation_names)
    marker = root / MANIFEST
    if not marker.exists() and not marker.is_symlink():
        if found:
            raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID")
        return
    try:
        manifest = json.loads(_bytes(marker), object_pairs_hook=_unique_object)
        if (
            not isinstance(manifest, dict) or set(manifest) != {"format_version", "application", "files"}
            or type(manifest["format_version"]) is not int or manifest["format_version"] != 1
            or manifest["application"] != application or not isinstance(manifest["files"], dict)
            or set(manifest["files"]) not in accepted_names or found != set(manifest["files"])
        ):
            raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID")
        for name, digest in manifest["files"].items():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID")
            if hashlib.sha256(_bytes(root / name)).hexdigest() != digest:
                raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID")
    except (ValueError, TypeError, UnicodeError):
        raise GitBuildError("VERCEL_GIT_EXISTING_OUTPUT_INVALID") from None


def _publish(stage, root, previous):
    previous.mkdir()
    moved = []
    installed = []
    try:
        for name in (*DIRECTORIES, MANIFEST):
            target = root / name
            if target.exists():
                target.rename(previous / name)
                moved.append(name)
            (stage / name).rename(target)
            installed.append(name)
    except Exception:
        for name in reversed(installed):
            (root / name).rename(stage / name)
        for name in reversed(moved):
            (previous / name).rename(root / name)
        raise


def _build_git(application, project_root):
    if application not in packaging.ASSETS:
        raise GitBuildError("VERCEL_GIT_INVALID_APPLICATION")
    if os.environ.get("MEAL_APPLICATION", application) != application:
        raise GitBuildError("VERCEL_GIT_APPLICATION_MISMATCH")
    project_root = Path(project_root).absolute()
    root = project_root / "apps" / application
    _scaffold(root, project_root, application)
    lock = root / ".git-build.lock"
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise GitBuildError("VERCEL_GIT_BUILD_IN_PROGRESS") from None
    os.close(descriptor)
    try:
        sources = packaging._runtime_sources(project_root)
        names = set(sources)
        names.update("frontend/dist/" + application + "/" + name for name in (*packaging.ASSETS[application], "index.html"))
        names.update("public/assets/" + name for name in packaging.ASSETS[application])
        _existing(root, application, names)
        with tempfile.TemporaryDirectory(prefix=".meal-git-build-" + application + "-", dir=root.parent) as temporary:
            temporary = Path(temporary)
            built = temporary / "frontend"
            packaging._build_frontend(project_root, built)
            for name in (*packaging.ASSETS[application], "index.html"):
                content = packaging._regular_bytes(built / application / name)
                if not content:
                    raise GitBuildError("VERCEL_GIT_SOURCE_OR_FRONTEND_INVALID")
                sources["frontend/dist/" + application + "/" + name] = content
                if name != "index.html":
                    sources["public/assets/" + name] = content
            stage = temporary / "package"
            for name, content in sources.items():
                packaging._write(stage / name, content)
            manifest = {
                "format_version": 1, "application": application,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(sources.items())},
            }
            packaging._write(stage / MANIFEST, (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8"))
            _existing(stage, application, names)
            _scaffold(root, project_root, application)
            _existing(root, application, names)
            _publish(stage, root, temporary / "previous")
        return {"application": application, "file_count": len(sources), "bytes": sum(len(content) for content in sources.values())}
    finally:
        lock.unlink()


def build_git(application, *, project_root=PROJECT_ROOT):
    try:
        return _build_git(application, project_root)
    except GitBuildError:
        raise
    except packaging.PackagingError:
        raise GitBuildError("VERCEL_GIT_SOURCE_OR_FRONTEND_INVALID") from None
    except Exception:
        raise GitBuildError("VERCEL_GIT_BUILD_FAILED") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build-vercel-git")
    parser.add_argument("application", choices=tuple(packaging.ASSETS))
    arguments = parser.parse_args(argv)
    try:
        result = build_git(arguments.application)
    except Exception as error:
        code = str(error) if type(error) is GitBuildError and str(error) in ERROR_CODES else "VERCEL_GIT_BUILD_FAILED"
        print(code, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
