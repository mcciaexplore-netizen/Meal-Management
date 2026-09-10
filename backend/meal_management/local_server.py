import argparse
import errno
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .errors import DomainError
from .runtime import RuntimeSettings


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _port(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Port must be an integer between 0 and 65535.") from None
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("Port must be an integer between 0 and 65535.")
    return number


def _environment(path, *, file_only=False):
    from dotenv import dotenv_values

    if not path.is_file():
        raise DomainError("LOCAL_ENV_FILE_REQUIRED")
    values = {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}
    if not file_only:
        values.update(os.environ)
    return values


def _configuration_message(error):
    if error.code == "LOCAL_ENV_FILE_REQUIRED":
        return "Environment file is missing. Create .env from .env.example or choose --env-file."
    if error.code == "LOCAL_SERVER_REQUIRES_DEVELOPMENT":
        return "This launcher requires APP_ENV=development. Use the production deployment instructions for production."
    if error.code == "LOCAL_ORIGIN_REQUIRED":
        return "Local application origins must use http://localhost or http://127.0.0.1."
    if error.code in {"ADMIN_FRONTEND_BUILD_REQUIRED", "SCANNER_FRONTEND_BUILD_REQUIRED"}:
        application = "Admin" if error.code.startswith("ADMIN_") else "Scanner"
        return application + " frontend assets are missing or incomplete. Build the frontend before starting this application."
    names = getattr(error, "missing", ())
    if names and all(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in names):
        return "Missing configuration: " + ", ".join(names) + "."
    code = error.code if isinstance(error.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", error.code) else "INVALID_CONFIGURATION"
    return "Configuration error: " + code + ". Check the example environment settings."


def _application(application, settings, runtime):
    from .application import create_services

    if application == "admin":
        from .admin_api import create_app
    else:
        from .scanner_api import create_app
    return create_app(services=create_services(settings), runtime=runtime)


def _validate_assets(application):
    directory = PROJECT_ROOT / "frontend" / "dist" / application
    names = ("index.html", "styles.css", "app.js") if application == "admin" else (
        "index.html", "styles.css", "scan-only.css", "scan-app.js",
    )
    if any(not (directory / name).is_file() or (directory / name).stat().st_size == 0 for name in names):
        raise DomainError(application.upper() + "_FRONTEND_BUILD_REQUIRED")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="meal-local-server")
    parser.add_argument("application", choices=("admin", "scanner"))
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--env-file-only", action="store_true", help="Use application settings only from the selected environment file.")
    parser.add_argument("--port", type=_port)
    arguments = parser.parse_args(argv)
    listener = None
    requested_port = arguments.port
    try:
        environment = _environment(arguments.env_file.expanduser().resolve(), file_only=arguments.env_file_only)
        os.chdir(PROJECT_ROOT)
        settings = Settings.from_env(environment)
        runtime = RuntimeSettings.from_env(environment)
        selected = runtime.for_application(arguments.application)
        if selected.environment != "development":
            raise DomainError("LOCAL_SERVER_REQUIRES_DEVELOPMENT")
        origin = urlsplit(selected.app_origin)
        if origin.scheme != "http" or origin.hostname not in {"localhost", "127.0.0.1"}:
            raise DomainError("LOCAL_ORIGIN_REQUIRED")
        _validate_assets(arguments.application)
        if requested_port is None:
            requested_port = origin.port or 80
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", requested_port))
        listener.listen(2048)
        port = listener.getsockname()[1]
        key = "APP_ORIGIN" if arguments.application == "admin" else "SCANNER_ORIGIN"
        environment[key] = "http://" + origin.hostname + ":" + str(port)
        runtime = RuntimeSettings.from_env(environment)
        app = _application(arguments.application, settings, runtime)
        from uvicorn import Config, Server

        config = Config(app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False, log_level="info")
        server = Server(config)
        print(arguments.application.capitalize() + " application: " + environment[key] + "/", flush=True)
        print("Press Ctrl+C to stop this application. The other application runs independently.", flush=True)
        server.run(sockets=[listener])
        return 0 if server.started else 1
    except DomainError as error:
        print(_configuration_message(error), file=sys.stderr)
        return 1
    except ImportError:
        print("A project dependency is missing. Install the approved requirements in the project .venv.", file=sys.stderr)
        return 1
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print("Port " + str(requested_port) + " is already in use. Choose --port or stop its owner yourself.", file=sys.stderr)
        else:
            print("The local server could not open its environment file or loopback listener.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("The local application could not start. Check configuration and installed project dependencies.", file=sys.stderr)
        return 1
    finally:
        if listener is not None:
            listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
