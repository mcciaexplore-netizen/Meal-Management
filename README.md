# Meal management

A Python application for reusable employee and office master QRs. The backend uses FastAPI, Uvicorn, and MySQL 8.4 with InnoDB. The administration dashboard and separate scan-only application are built from JavaScript and CSS in this same project. MySQL remains the database for local development; there is no SQLite substitute and no employee-count limit.

Exactly two QR categories exist: `EMPLOYEE` and `MASTER`. Both record reusable, deliberate servings. The scanner application at `http://localhost:8001` opens without staff login, location selection, or service-station selection. Master scanning collects Company Name, Name, Email, and Phone and records one meal without per-serving administrator approval. Historical administrator authorizations remain intact. There are no individual guest credentials or guest single-use rules.

The administrator application at `http://localhost:8000` and scanner application at `http://localhost:8001` run as independent FastAPI/Uvicorn processes. Each serves its own screens and APIs. Both connect directly to the same MySQL database through shared Python services; the scanner never calls the admin server. Stopping the admin process does not stop scanner meal recording. No third application server or second meal database is needed.

Local development uses private photo files and authenticated email previews. AWS resources and real email delivery are not required to develop or review the application. MySQL integration and deployment checks remain separate verification gates; this project must not be treated as production ready while those checks remain unverified.

## Project layout

| Path | Purpose |
| --- | --- |
| `backend/meal_management/admin_api.py` | Administrator application entry point; protected routes implemented in `api.py` |
| `backend/meal_management/scanner_api.py` | Independent scanner application with only scanner routes, health checks, and scanner assets |
| `backend/meal_management` | Existing transactional services, authentication, reports, configuration, storage, email adapters, and CLI |
| `manage.py` | Project-local CLI entry point with an explicit backend import path |
| `frontend/src` | Login, administration, employee management, QR display, visitor approvals, scanning, and reporting screens |
| `frontend/dist/admin`, `frontend/dist/scanner` | Separate generated browser assets served only by their corresponding application |
| `frontend/tests` | Browser-independent frontend state and validation tests |
| `database/migrations` | Ordered, checksummed database migrations |
| `database/schema.sql` | Current schema snapshot for disposable integration databases |
| `database/README.md` | Relationships, constraints, and transaction responsibilities |
| `tests/unit` | Service, authentication, HTTP/API, configuration, storage, migration, and source-policy tests |
| `tests/integration` | MySQL tests requiring explicit connection and disposable-schema approval |
| `tests/local` | Independent loopback process startup tests using fictional configuration and no database access |
| `deploy` | Separate foreground admin/scanner startup scripts and an example process-supervision configuration |
| `docs/deployment.md` | Local setup details, existing-database migration procedure, and later deployment decisions |
| `docs/api.md` | HTTP endpoint and request-behavior reference |
| `.env.example` | Example settings with blank secret values and integration tests disabled |

## Local setup

Use Python 3.11 or newer, a supported Node.js installation with npm, and MySQL 8.4. Run commands from the project root. Dependency installation, database access or changes, real email sending, and AWS access are separate operator-approved actions; none happen automatically through a configuration check or frontend build.

Once dependency installation is approved, create a Python environment and install the declared application and test dependencies:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
npm --prefix frontend ci
npm --prefix frontend run build
```

The frontend uses locally bundled `jsqr` for QR decoding and `esbuild` for its build. It does not depend on a public CDN at runtime. `npm ci` uses the checked-in lockfile. Rebuild after editing frontend sources.

Create a private `.env` from `.env.example` and fill in the required values. Do not commit `.env`, credentials, encryption keys, photo files, or rendered QR/email content. The CLI loads a file only when `--env-file` is supplied; the local startup script explicitly loads `.env`.

Run CLI commands through `manage.py` from the project root. It adds the project's absolute backend directory to the import path, so local management commands do not depend on editable-install `.pth` discovery. The startup scripts also resolve the project and backend import paths explicitly.

| Setting | Local-development requirement |
| --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME` | An existing MySQL 8.4 database and its address |
| `DB_USER`, `DB_PASSWORD` | Private database credentials; use separate migration and runtime accounts |
| `DB_SSL_CA` | Certificate authority file when database TLS verification is required; required for production |
| `QR_ENCRYPTION_KEYS` | One or more generated Fernet keys, comma-separated, with the newest first |
| `APP_CSRF_SECRET`, `LOGIN_RATE_SECRET` | Separate cryptographically generated secrets of at least 32 bytes |
| `APP_ORIGIN` | The exact administrator browser origin, initially `http://localhost:8000` |
| `SCANNER_ORIGIN` | The exact separate scanner origin, default `http://localhost:8001` when omitted |
| `ALLOWED_HOSTS` | Explicit permitted hostnames for both origins, locally `localhost,127.0.0.1` |
| `PHOTO_BACKEND`, `PRIVATE_PHOTO_ROOT` | `local` and a private canonical filesystem directory |
| `EMAIL_BACKEND`, `EMAIL_SEND_ENABLED` | `preview` and `false` |

Generate QR encryption keys using `cryptography.fernet.Fernet.generate_key()` after that dependency is installed. Generate the two application secrets independently with a cryptographic random generator. Configuration accepts those secrets as strings or `base64:` followed by encoded random bytes. Blank settings fail validation; there is no default password or usable secret. Preserve old encryption keys while stored credentials still depend on them, because secure resending requires decryption of the original token.

Validate settings and inspect migration files without connecting to MySQL:

```sh
.venv/bin/python manage.py --env-file .env check-config
.venv/bin/python manage.py migration-plan
```

The migration runner does not create a database. After an operator creates the intended empty database and approves database changes, apply the versioned migrations:

```sh
.venv/bin/python manage.py --env-file .env migrate --allow-database-changes
```

Do not run `database/schema.sql` over an existing installation or baseline an arbitrary schema. The migration runner refuses an existing untracked schema. Back up and review earlier databases using the [existing-database procedure](docs/deployment.md#versioned-migrations), which preserves records and requires a reviewed schema fingerprint before adopting the original migration.

After migrations, explicitly create the first administrator through the interactive CLI:

```sh
.venv/bin/python manage.py --env-file .env bootstrap-admin --allow-database-access
```

The command prompts for the administrator's name, email, and password, asks for password confirmation, and does not echo the password. It refuses non-interactive input and refuses to initialize an administrator after any human staff account exists. The built-in shared scanner account does not prevent this explicit first-admin setup. There is no public administrator-registration endpoint.

Start the administrator in Terminal 1 from the project directory:

```sh
sh deploy/start-admin.sh
```

Start the scanner in Terminal 2 from the same project directory:

```sh
sh deploy/start-scanner.sh
```

Open `http://localhost:8000` for administration and `http://localhost:8001` for scanning. Keep both terminals open; Control+C stops only the process in that terminal. If an older server occupies port 8000, stop that server in its own terminal before starting the administrator. The scripts report occupied ports without terminating other processes. `start-local.sh` remains an administrator-only compatibility alias.

Both scripts load the same private `.env`, bind only `127.0.0.1`, disable access logs and proxy-header trust, and run their own application in the foreground. They do not apply migrations, insert defaults, or launch a second process. Email delivery is disabled by default; after explicit configuration, the admin process can run the automatic sender in its own background thread. The scanner never starts email delivery. Configuration and asset errors are reported before serving. Each application has its own `/health/live`, which makes no database query. `/health/ready` checks MySQL and the migration ledger, so requesting it requires approved database access.

For phone camera testing, obtain separate approval for network exposure and trusted HTTPS configuration first. The loopback startup address is not reachable from another device. A phone opening a computer's HTTP LAN address does not receive the browser's localhost camera exception. See [development fixtures and laptop/phone testing](docs/development-testing.md) for the guarded seed command, QR gallery, and separate device checklists.

## First-use setup and permissions

Sign in as the initialized administrator. Create departments, meal types, serving locations, and scanner devices through the configuration screens. Each scanner belongs to a location. Then create named staff accounts with individual passwords and the roles they need.

The server enforces roles on every protected endpoint and again inside services. `ADMIN` permits employee and staff management, credential management, visitor authorization, unrestricted reports, and scanning. `WAITER` permits scanning and access to that waiter's assigned visitor authorizations. Helpers use the existing `WAITER` role. Both roles can view their own committed serving receipts and available employee photos without gaining access to another staff member's receipts. Existing `AUDITOR` support remains in the database services, but the application's administration and unrestricted report endpoints require `ADMIN`.

Staff identity is resolved from a hashed server-side session token, not from a role or staff identifier supplied in a scan. Passwords use salted scrypt hashing. Sessions have absolute expiry, configurable idle expiry, revocation, and HTTP-only SameSite cookies. Production requires secure `__Host-` cookies and HTTPS. Changes require an exact allowed origin and a session-bound CSRF token. Login attempts are limited by shared database buckets for the account and client address. The last active administrator cannot be disabled or lose the administrator role through the staff service.

## Employee management and reusable QRs

Employee registration stores the code, name, required email, department, active status, and optional selfie reference. Registration creates the employee, random QR, encrypted queued email, and audit entries in one transaction. Each employee has at most one unrevoked QR, and their QR can record any number of deliberate meals over time.

Employee registration and editing offer these facility departments: Prototype Production Facility, Rapid Prototype Centre, Environmental Testing, Rubber & Polymer Testing Lab, Large Bed CMM & Metrology Services, Exhibition Centre, Auditorium, and Training & Seminar Hall. Existing department IDs and other active departments remain available. A missing selected facility department is created through the existing administrator-only catalog API when the employee form is submitted, then its confirmed database ID is used for the employee. Opening the form creates no records. Inactive departments are not reactivated automatically. Department creation is a separate transaction and remains saved if the subsequent employee submission fails; retrying reuses it.

QRs contain random bearer tokens, never names or employee codes. MySQL stores a SHA-256 digest for lookup and an encrypted recoverable token for administrator-only display and resending. Resending queues the same active QR; it does not generate another credential. Issuance refuses an existing unrevoked QR atomically. Replacement revokes the old credential and queues the replacement in the same transaction. Revocation removes recoverable credential material and cancels pending delivery. Expired credentials must be replaced or revoked before another current credential is issued.

Deactivation blocks new employee meals and cancels pending emails without deleting employee or meal history. An administrator can reactivate an employee. Name and email changes preserve the employee identifier; changing email cancels stale delivery and queues the current usable QR to the new address.

Photo uploads accept bounded JPEG or PNG content, decode and re-encode it to remove embedded metadata, and save a private storage object. MySQL stores its reference only. Private photo and QR retrieval routes require administrator authentication; neither is published as a public static asset.

The registration dialog supports **Take photo** alongside file upload. It prefers the front camera, shows an inline preview, and requires **Use photo** before a capture is selected. **Retake** and **Cancel** preserve any previously selected photo until a replacement is accepted. The captured JPEG is uploaded through the existing private photo endpoint after successful employee registration; choosing or capturing a photo alone does not upload it. If the photo upload fails after registration, the employee remains registered and the interface reports the failure. Closing or leaving the dialog, hiding the page, or finishing capture releases camera access. Camera permission and hardware behavior still need verification on the target browsers.

## Visitor meals and the master QR

The admin office manages a reusable `MASTER` QR. Open the scanner at `http://localhost:8001`. It opens directly to the camera controls with no login, account controls, or location/service-station questions. The administration dashboard remains at `http://localhost:8000/#dashboard` and still requires an administrator login. The scanner process serves neither administrator pages/assets nor administrator APIs. Its HTTP-only browser cookie and CSRF proof do not authenticate a staff session. Each server validates its own exact origin; no cross-origin API calls or privileged browser credentials connect the applications.

The backend supplies the default `Meal` type and `Meal Scanner` location/counter. A dedicated shared `Meal Scanner` system identity attributes these records without claiming that an individual waiter signed in. It has no password and cannot receive staff sessions or administrator roles. The backend determines the QR category. A valid employee scan commits one meal and displays **Accepted · 1 meal**. A valid master read opens exactly four visitor fields: Company Name, Name, Email, and Phone. Submitting that form records one visitor meal and its UTC time. No per-serving admin approval is required. The dashboard's meal history displays the four saved visitor fields.

The visitor form appears only after server validation. Opening it does not approve or record a meal. QR lifecycle and serving settings are checked again when the form is submitted. Migration 004 adds immutable visitor fields and request detail binding. Migration 005 adds the shared scanner identity, defaults, and immutable browser-to-request ownership records. Both preserve old meals and authorizations. The earlier staff-authenticated scan and authorization APIs remain compatible with historical prepared servings; the public scan interface does not use them.

The scanner automatically receives its own signed, HTTP-only browser cookie and CSRF token. No staff account is authenticated. Retried operations are bound to that browser and the original request, while the backend selects all service settings. Browser and address rate limits use shared database buckets. `SCAN_APP_ENABLED` defaults to true in development and false in production; production requires explicit enabling. Anyone who can reach the enabled scanner can submit a valid meal QR, so decide its intended network reach before later hosting. This change does not expose the development server beyond loopback.

## Scanning, retries, and uncertain results

The scanner screen starts one request UUID for an intended serving. Repeated frames and network retries retain that UUID and the original input. Only an explicit new-serving action creates another UUID. Employee servings always have quantity one; employees can deliberately begin another serving on the same day with a new request.

The backend commits a scan receipt before processing. It then validates eligibility and atomically saves the successful scan, serving, individual meal rows, and final request result. It returns approval only after a successful commit. Business rejections are recorded with their reason and context. Authenticated scan-body validation failures also receive rejected scan records. Requests that cannot reach the database cannot be logged or approved.

The shared MySQL database stores serving details in `servings`, one row for each actual meal in `meals`, and scan outcomes/reasons in `scan_attempts`. `serving_requests` and `scan_app_requests` preserve idempotency and browser ownership for recovery. The admin **Overview** shows totals; **Meal history** shows employee or visitor fields, the configured scanner actor, meal type, and timestamps. **Rejected scans** shows reasons and scan context. Reopen or refresh the report to load newly recorded meals. UTC timestamps are displayed in the browser's local timezone.

| Scanner state | Current message and effect |
| --- | --- |
| Employee QR approved | `Accepted · 1 meal`, after the database commit |
| Master QR validated | `Visitor meal` with Company Name, Name, Email, and Phone; no meal is recorded until valid submission |
| Visitor form committed | `Accepted · 1 meal` |
| Rejected QR | `Not accepted` plus a reason such as `unknown qr`, `qr expired`, `qr revoked`, or `employee inactive` |
| Processing | `Checking QR…` or `Recording meal…` |
| Lost or uncertain response | `Result not confirmed`; retry or check the same serving before continuing |
| Previously committed result recovered | `Accepted · 1 meal` with `Already recorded. Do not serve again.` |

After a completed serving, scanning pauses until **Scan next meal** starts a new request. An employee can then deliberately use the same QR for another meal.

An exact successful retry returns `approved=true`, `code=APPROVED`, the original serving and meal identifiers, and `duplicate=true`. It confirms an existing recorded serving and must not cause another meal to be handed out. The additional scan attempt is logged as `DUPLICATE_REQUEST`, so rejected-scan reports also distinguish repeated reads from new servings. An exact rejected retry returns its original rejection. Changing the authenticated caller, credential, scanner code, meal type, quantity, submitted visitor details, or bound legacy approval proof under the same UUID is rejected.

Completed retries verify the original receipt's scanner and location, so relocating a scanner later does not invalidate an already committed result. The repeated attempt still records its current location. Pending approvals continue to require their approved location. Legacy requests lacking trustworthy original receipt or authorization-proof metadata return `IDEMPOTENCY_PROOF_UNAVAILABLE` rather than claiming a verified replay.

If the result is unconfirmed, retain the current request and use the retry action. A browser-bound session-storage marker preserves the UUID and quantity one across reloads; QR credentials and visitor contact details remain only in memory. The scanner recovers the original committed result or requires the original QR to be read again with the same identifier. An unresolved marker from a different scanner cookie scope blocks new meals until its history is checked. Closing the browser session or clearing all its storage can remove this marker; in that case an administrator must check meal history before a new serving starts. The camera pauses after a completed serving, and only the explicit **Scan next meal** action begins another meal. Camera and QR-image upload both use the same backend validation.

When local recovery storage is missing but the scanner cookie remains, startup looks up the last browser-owned request in MySQL before enabling scanning. This also covers moving from the former port 8000 scanner to port 8001, whose browser storage is separate. A recovered result may already have been served and requires **Scan next meal** to deliberately continue. A failed lookup blocks startup. Before switching from the old server, finish or reconcile pending servings and preserve the scanner cookie and application secrets. If both cookie and local storage are removed, an administrator must check history because ownership cannot be recovered automatically.

## Reports, email status, and local previews

Administrators can browse individual meals, employee history, meal totals and breakdowns, rejected scans, and email status. Lists use ascending identifier cursors with `after_id`, `limit`, and `next_cursor`; limits are validated. Date filters must include a timezone offset. They are converted to UTC and use an inclusive start and exclusive end. The screens convert selected local date boundaries to UTC for API requests.

Every meal has its own timestamp. Serving history retains employee, waiter, location, meal type, and any historical authorization identifiers. Direct visitor company, name, email, and phone are immutable snapshots saved with the serving. Descriptive names from reference records reflect their current values. Successful servings, meal rows, finalized scans, and audit events are protected against ordinary updates and deletion. A restricted runtime database account is still required because schema-changing privileges can bypass those protections.

With `EMAIL_BACKEND=preview`, employee emails remain in the encrypted queue and can be opened through administrator-only previews. Opening a preview sends no message and does not mark delivery as sent. Revocation or an email-address change invalidates stale previews. The status list exposes safe metadata rather than encrypted payloads or credential hashes.

Gmail delivery uses an App Password over verified TLS. After approval, set `EMAIL_SEND_ENABLED=true` and `EMAIL_AUTO_SEND_ENABLED=true` to process queued employee QR emails automatically inside the admin server. Registration and resends commit their queued messages before the sender can see them. `EMAIL_POLL_SECONDS` defaults to 5 and `EMAIL_BATCH_SIZE` to 10. The admin email queue shows whether the automatic sender is running. Stopping the admin server leaves pending emails in MySQL for its next start; the scanner continues independently.

The sender creates a durable claim before each provider call and records acceptance only after a successful database commit. Failed or uncertain sends require review and are never automatically retransmitted. Global sender or authentication failures halt automatic processing until configuration is corrected and the admin server is restarted. Real sending and automatic processing remain disabled by default. The guarded `send-email` CLI command remains available for a separately approved single-message test, and no browser send-now endpoint is exposed. See [Gmail configuration and sending instructions](docs/gmail-setup.md).

Optional S3 and SES adapters remain available. Production fails configuration validation if local photo storage or email previews are selected; it never silently falls back to development substitutes. Managed retries of known failures, delivery receipts, provider quotas, and bounce handling still require implementation and verification before unattended production delivery. See [deployment preparation](docs/deployment.md).

## Tests and verification boundaries

The latest executed checks and remaining verification gates are recorded in [verification results](docs/verification.md).

Run the local Python suite after installing the declared test dependencies:

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests/unit -v
```

HTTP/API tests are part of this suite and use injected services instead of a database connection. The suite also covers service permissions, both QR categories, request binding and retries, transaction failures, configuration validation, private storage, email preview behavior, migration safety, and the source policy. Install the declared test dependencies before running API tests. A reported skip is not a pass. Test commands set `PYTHONPATH=backend` explicitly so discovery also works when editable-install path files are not loaded.

Run frontend state and validation tests without contacting a backend:

```sh
npm --prefix frontend test
```

Discover MySQL tests with database access explicitly disabled:

```sh
PYTHONPATH=backend MEAL_RUN_MYSQL_TESTS=0 MEAL_ALLOW_TEST_SCHEMA_CHANGES=0 .venv/bin/python -m unittest discover -s tests/integration -v
```

Both MySQL flags are zero in `.env.example`. Real integration execution requires a separately approved MySQL 8.4 test server, loaded test database settings, dependencies, and explicit authorization to create and drop isolated test schemas. The fixture requires both flags to equal `1` and a `MEAL_TEST_DB_NAME` prefix ending in `_test`. It creates a unique disposable schema and drops that schema afterward; do not use production credentials or enable these flags casually. Test discovery does not automatically load `.env`.

After those approvals and configuration, the explicit integration command is:

```sh
PYTHONPATH=backend MEAL_RUN_MYSQL_TESTS=1 MEAL_ALLOW_TEST_SCHEMA_CHANGES=1 MEAL_TEST_DB_NAME=meal_management_test .venv/bin/python -m unittest discover -s tests/integration -v
```

Unit and API tests do not establish that MySQL accepted the schema, that real locking behavior matches the design, or that migrations preserve a populated installation. MySQL scenarios include simultaneous retries, simultaneous deliberate meals, master approval scope, scanner relocation, QR issuance races, reporting, immutable proof binding, and rollback behavior. MySQL execution, target-phone camera checks, AWS adapters, live email delivery, infrastructure configuration, and deployment must be verified separately.

## Source and deployment policy

Application source files contain no comments or docstrings. Explanations and operating instructions belong in Markdown documentation. Keep credentials and QR material out of source, logs, screenshots intended for sharing, and public build assets.

No AWS resources are created by this project. Decisions still needed include EC2-hosted MySQL versus RDS, application compute, private networking, TLS and DNS, secrets and key rotation, private S3 policy, SES setup and worker design, database backups and restore drills, monitoring, and release rollback procedures. Follow the separate [deployment guide](docs/deployment.md) before enabling production settings or deploying.
