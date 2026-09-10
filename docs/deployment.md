# Local development and later deployment

For the first hosted-database phase without personal AWS credentials, follow [Aiven MySQL setup](aiven-setup.md). It reuses the existing database adapter and keeps the local connection unchanged until the hosted target and data transfer are approved.

No database is created or migrated automatically. Starting the application does not create AWS resources or initialize an administrator. Email delivery is disabled by default; the admin process starts its background sender only after both real-sending and automatic-delivery settings are explicitly enabled. The scanner never starts a sender. AWS adapters are optional and do not connect while being constructed. This project is not production ready until the MySQL integration suite and infrastructure checks below are completed.

## Local preparation

Use Python 3.11 or later and MySQL 8.4. MySQL remains the database in local development. Do not substitute SQLite because its locking, constraints, and SQL behavior differ.

From the project directory, create a virtual environment and install the application and test extras only when installation is approved:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Copy `.env.example` to `.env` yourself and keep the file private. Blank values deliberately prevent startup. Set `DB_HOST`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` for a local MySQL 8.4 database. The database must already exist; the migration runner does not create databases. Provision a migration account separately from the application's restricted runtime account. Do not commit `.env` or print its values.

Set `QR_ENCRYPTION_KEYS` to one or more comma-separated Fernet keys; the first encrypts new credentials, and later keys decrypt existing records during key rotation. Generate the keys securely and retain old keys until their encrypted records have been rotated. The encrypted QR value is required to resend the same active QR. Losing all applicable keys makes those credentials unrecoverable and requires replacement.

Set `APP_CSRF_SECRET` and `LOGIN_RATE_SECRET` to separate cryptographically generated secrets with at least 32 bytes each. Each value can be a plain string of at least 32 UTF-8 bytes or `base64:` followed by the Base64 encoding of at least 32 random bytes. Length validation cannot prove entropy; do not use repeated characters or memorable phrases. Keep these values stable across workers and restarts. They are excluded from configuration representations.

The default administrator origin is `APP_ORIGIN=http://localhost:8000`; the separate scanner defaults to `SCANNER_ORIGIN=http://localhost:8001` even when the latter is omitted from an existing `.env`. Both processes use the same database settings. The browser must use the exact origin for the selected application; `http://127.0.0.1:8000` is different from `http://localhost:8000` even if both hostnames are permitted. `ALLOWED_HOSTS` must include both applications' hostnames without wildcards. Local HTTP requires `COOKIE_SECURE=false`. Phone testing requires separately approved network and HTTPS configuration; the local launchers deliberately accept only loopback HTTP origins.

Check configuration without opening a database connection:

```sh
.venv/bin/python manage.py --env-file .env check-config
.venv/bin/python manage.py migration-plan
```

Run these commands from the project root. `manage.py` and the startup scripts explicitly supply the absolute backend import directory. The example production process configuration uses Uvicorn's `--app-dir backend` for the same reason.

## Versioned migrations

`database/migrations/001_initial.sql` preserves the original two-category schema. `002_authentication_and_retry_binding.sql` adds session idle activity, shared database login rate limits, and immutable scan authorization binding. It revokes existing sessions so users log in under the new session rules. It preserves employees, QRs, authorizations, scans, and meals. Old finalized scan requests that lack trustworthy original authorization binding cannot be replayed as verified modern requests; they remain in history.

`003_development_test_data.sql` adds fixture ownership records and encrypted credential archives for the development-only QR gallery. It inserts no test employees or accounts. The interactive seed command and separate laptop/phone exercises are documented in [development testing](development-testing.md); confirm the target development database before inserting fixtures.

`004_direct_master_visitor_meals.sql` adds four visitor snapshot fields for direct single-meal master servings, immutable visitor-detail request binding, and an `AWAITING_DETAILS` scan outcome. It preserves historical administrator-authorized servings and does not update existing meal data.

`005_shared_meal_scanner.sql` adds a dedicated `Meal Scanner` system account with no password, immutable browser-to-request attribution, and server-selected default scanner/location and `Meal` type. It inserts configuration records without overwriting existing accounts or catalog rows. A reserved-code collision fails for review. Database triggers prohibit scanner staff sessions, administrator roles, and changing account kind. Human first-admin setup ignores the shared system account. Readiness requires migrations 001 through 006. Apply pending migrations only after the target and schema changes are approved, then restart the applications. Splitting the processes changed none of the five original migration files. Migration `006_employee_email_approval.sql` separately adds bulk import identity, immutable approval attribution, and single draft/pending approval states. It holds existing queued messages for review and preserves sent history. Stop the old admin process before applying it; do not let the old sender run against a partially updated schema. The live migration ledger must be inspected with separate approval to determine what remains pending.

Before replacing the old combined server, finish or reconcile pending servings and stop that server in its own terminal. Preserve the scanner cookie, `APP_CSRF_SECRET`, and hostname. The new scanner can recover its latest database-mapped request when port-scoped browser storage is missing; it never substitutes a new UUID for a recovered uncertain serving. Removing both the cookie and browser storage removes recovery ownership and requires an administrator to inspect history.

`database/schema.sql` is a current schema snapshot for disposable integration databases. For an application database, use the migration runner instead of executing that snapshot. The runner serializes migration processes, records file checksums, and refuses changed applied migrations. Do not edit a published migration after it has been applied; add a new version.

Keep `.env` configured with the restricted runtime database account. The `schema-inspect`, `migrate`, and `baseline` commands accept optional `--database-user USER` to use a different MySQL account for that operation. When supplied, the command requires an interactive terminal and securely prompts for that account's password without echoing it. It overrides `DB_USER` and `DB_PASSWORD` in memory only, retains the configured host, database, TLS, and other settings, and never writes the supplied credentials to `.env` or another file. Without this option, the commands use the configured database credentials.

The selected account must have the privileges required for the operation. `--database-user` does not replace the explicit access or change flag: `schema-inspect` still requires `--allow-database-access`, while `migrate` and `baseline` still require `--allow-database-changes`.

For initial local Homebrew setup, after the empty database exists and database changes are approved, the local MySQL `root` account can be used interactively:

```sh
.venv/bin/python manage.py --env-file .env migrate --database-user root --allow-database-changes
```

Enter that MySQL account's actual password when prompted; there is no assumed or default password. Production migrations should use a dedicated migration account instead of `root`, while the application continues using the restricted account in its private environment file.

An existing database without a migration ledger is rejected, preserving its records. Back it up and compare its tables, columns, indexes, constraints, triggers, reference rows, and settings against migration 001. If the database is the earlier schema and that comparison is approved, inspect it and record the reviewed fingerprint. Replace `meal_migrator` below with the approved migration account's username; each command prompts for its password:

```sh
.venv/bin/python manage.py --env-file .env schema-inspect --database-user meal_migrator --allow-database-access
.venv/bin/python manage.py --env-file .env baseline --database-user meal_migrator --version 1 --expected-schema-fingerprint REVIEWED_FINGERPRINT --allow-database-changes
.venv/bin/python manage.py --env-file .env migrate --database-user meal_migrator --allow-database-changes
```

Baselining only adopts migration 001 and requires the exact table inventory and the same live definition fingerprint the operator reviewed. It does not prove the schema matches migration 001: the operator must perform that comparison before approval. The fingerprint excludes auto-increment counters, so normal inserts do not change it. Do not baseline a database created from the new current schema snapshot as version 1. For any other existing schema, prepare a reviewed conversion migration instead of dropping or recreating tables.

MySQL DDL commits independently of surrounding application transactions. A failed migration remains marked `APPLYING`; automatic retries are refused because some statements may already have committed. Inspect the database and restore a backup or complete a reviewed repair before correcting the ledger. Do not mark a migration applied merely to suppress the error. Use a maintenance window for migrations; the migration lock coordinates migration runners, not application requests.

If a migration command fails, inspect the database before retrying changes. For local setup, run the following command yourself in an interactive terminal:

```sh
.venv/bin/python manage.py --env-file .env schema-inspect --database-user root --allow-database-access
```

This opens a database connection and reads schema metadata and the migration ledger. It acquires and releases the migration advisory lock but does not create tables, apply migrations, or change application records. It prints table names, a schema fingerprint, and migration statuses. Share that output or the safe error message for diagnosis; do not share passwords or `.env` values.

The CLI reports numeric MySQL error codes with fixed explanations for common authentication, access, connection, missing-database, and TLS failures. It never prints raw driver error messages or SQL. Code meanings follow the [MySQL server error reference](https://dev.mysql.com/doc/mysql-errors/8.4/en/server-error-reference.html) and [client error reference](https://dev.mysql.com/doc/mysql-errors/8.4/en/client-error-reference.html). A generic failure from an earlier CLI version does not establish whether the connection failed or some migration statements committed.

## First administrator and application startup

After migration, explicitly initialize the first administrator:

```sh
.venv/bin/python manage.py --env-file .env bootstrap-admin --allow-database-access
```

This interactive command prompts for a name, email, and password twice without echoing the password. It refuses non-interactive input and refuses to run once human staff accounts exist; the dedicated scanner system account does not block first-admin setup. There is no default administrator password or public administrator registration. The authenticated administrator can create waiter accounts through the application.

Start from the project directory:

```sh
sh deploy/start-admin.sh
```

Run `sh deploy/start-scanner.sh` in another terminal from the same project directory. The administrator runs on `http://localhost:8000/`; the scanner runs on `http://localhost:8001/`. Each script starts only its own foreground process. Both bind `127.0.0.1`, disable access logs and proxy-header trust, and validate configuration without connecting to MySQL. A port conflict reports the occupied port without terminating its owner. `start-local.sh` is now an admin-only alias. Keep both terminals open, and use Control+C to stop one application independently.

Each application serves only its own pages and assets. Scanner requests call shared Python services against MySQL directly, so the admin server is not needed to scan meals. Neither startup script applies migrations, inserts data, nor runs a sender. `/health/live` is separate process liveness without database access. `/health/ready` queries MySQL and its migration ledger and requires approved database access. Health errors do not disclose connection strings, credentials, SQL, or server errors.

The scanner has no login or service-selection screens. `SCAN_APP_ENABLED` defaults to true for development and false for production. Production must explicitly choose whether this public scanning surface is enabled and how its network is reached. It still requires valid QR credentials and grants no employee administration, credential export, photo, or unrestricted reporting access. Default shared settings are in `scan_app_settings`; they are chosen by the backend and cannot be overridden by scan requests. `SCAN_REQUEST_LIMIT`, `SCAN_IP_LIMIT`, and `SCAN_WINDOW_SECONDS` configure shared browser/address rate limits. The signed scanner cookie is separate from staff sessions and has a 30-day browser lifetime renewed on scanner startup; rotation of `APP_CSRF_SECRET` invalidates it. Reconcile pending browser operations before rotating that secret.

## Private local photos and email previews

`PHOTO_BACKEND=local` stores images under `PRIVATE_PHOTO_ROOT`; the default is `var/private/photos`. Files use unpredictable names and restrictive directory and file permissions. Storage accepts JPEG and PNG, decodes and re-encodes each photo to remove embedded metadata, limits payload size and decoded pixel count, and rejects symbolic links. Configure a canonical directory without symbolic-link path components. Photos are retrieved through authenticated application routes, never a public static directory.

`EMAIL_BACKEND=preview` keeps delivery in the existing encrypted MySQL queue and renders authenticated local previews. Opening a preview does not send an email or mark the queue item sent. Names and recipients are escaped, and the QR is embedded as an image rather than active HTML. Never put email previews, QR payloads, or selfies in public static directories or request logs.

The production `S3Storage` and `SESDelivery` adapters are configurable and use the AWS default credential provider chain. No AWS keys are stored in source. S3 writes request AES-256 server-side encryption and do not request public ACLs. The bucket must independently block public access. Gmail is also supported through verified TLS with a privately configured App Password and does not require AWS for local development. Production still requires production photo storage and secure database and browser connections.

`EMAIL_SEND_ENABLED=false` and `EMAIL_AUTO_SEND_ENABLED=false` remain the defaults. After approval, enabling real sending permits administrator-triggered single sends and approved bulk processing. Enabling both flags additionally starts polling of approved bulk/legacy entries inside the admin process, using `EMAIL_POLL_SECONDS` and `EMAIL_BATCH_SIZE`. Startup is nonblocking, shutdown stops new selection and drains the active attempt within its grace period, and the scanner stays independent. Pending entries survive admin restarts. Only approved bulk or legacy queued messages are selected; single drafts and pending approvals are excluded; global authentication failures halt processing, and temporary database/provider errors use bounded backoff. Failed or uncertain attempts are never automatically retried.

The guarded `send-email` command remains available for one selected queue entry with separate database-change and real-email flags. The shared worker commits its claim before sending and records provider acceptance after commit. The browser exposes an administrator-only Send email action scoped to one employee/email ID, plus bulk approval and bounded processing actions. See [Gmail setup](gmail-setup.md) for configuration, commands, and recovery. Managed retry policy, delivery receipts, provider quota handling, and bounce handling remain incomplete; a provider timeout can mean a message was accepted, so blind retries can send duplicates.

## Production configuration and decisions

Set `APP_ENV=production`, an HTTPS administrator `APP_ORIGIN`, a distinct HTTPS `SCANNER_ORIGIN` if the scanner is enabled, exact `ALLOWED_HOSTS`, and `COOKIE_SECURE=true`. The local scripts refuse production; use separately reviewed production process configuration. Production cookies use the `__Host-` prefix. Production requires `PHOTO_BACKEND=s3` and a real email backend (`gmail` or `ses`); configuration fails instead of silently using local substitutes. Supply `AWS_REGION`, `PHOTO_S3_BUCKET`, `EMAIL_SENDER`, and any Gmail App Password through private configuration. Real sending remains disabled until separately approved. Install the `production` dependency extra only when AWS integration testing is approved.

The example `deploy/meal-management.service.example` is an administrator-process template, not an installed service. A later scanner service must use `meal_management.scanner_api:create_app` in a separate process and listener. Review intended scanner network access before exposing its anonymous meal-writing surface. Adjust service users, paths, environment file, bind addresses, and worker counts after the hosting decision. Keep untrusted proxy headers disabled; configure trusted forwarding addresses explicitly if a later deployment needs them. Store the environment file outside the repository with mode 0600. Run migrations as a separate release step with a dedicated credential, never inside application startup. Package both frontend directories and the migration directory with the backend.

Choose and provision these items before deployment:

- EC2-hosted MySQL versus RDS MySQL 8.4. With EC2, the team operates database installation, upgrades, backups, recovery, storage, and failover. With RDS, define instance size, storage, availability, maintenance windows, backup retention, and recovery procedures. Validate the required version and configuration in the selected region when provisioning.
- Application compute, DNS, HTTPS certificates, private networking, security groups, and how administrators reach the service. Permit database access only from approved application and administration paths.
- Separate migration and runtime database users, TLS validation using `DB_SSL_CA`, and a managed secret distribution and rotation process. Production configuration must include database TLS settings; do not disable certificate verification.
- A private S3 bucket, server-side encryption policy, narrowly scoped IAM access, retention, photo deletion policy, and backup or recovery decisions. Use workload IAM roles instead of committed access keys.
- Decide whether production email uses Gmail/Workspace or SES. Verify account policy, quotas, sender/domain identity, delivery monitoring, bounce/complaint handling, scheduling, and the decision to enable real messages. SES additionally requires region and sandbox or production access configuration. Local previews do not verify either provider.
- Backup schedules and restore drills for MySQL, encrypted QR payloads and applicable encryption keys, photos, and audit history. Define employee data retention and log redaction rules.
- Process supervision, health monitoring, database capacity and connection limits, rate-limit table cleanup, alerting, and release rollback procedures. Deleting a rate-limit bucket too early can reset protection; clean only windows that have fully expired.

## Verification gates

Run local unit and API tests without enabling database integration. MySQL tests have separate explicit connection and disposable-schema guards. Review the integration setup and use a dedicated MySQL 8.4 test account authorized to create and drop only isolated test databases. Never use production credentials for integration tests.

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests/unit -v
```

The API test command and integration opt-in are documented in the project README. Before a release, run the MySQL integration suite, including concurrent scans and same-request retries, migrate a fresh database, migrate a backed-up copy of the older database, verify first-admin setup and session expiry, and test reports against realistic data. Verify camera scanning on target phones, authenticated private photo access, local email previews, and the HTTPS reverse proxy. S3, SES, IAM, real email delivery, backup restore, and deployment checks remain pending until credentials, infrastructure, and authorization are available. Do not call this application production ready while those required checks remain unverified.
