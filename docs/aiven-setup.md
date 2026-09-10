# Aiven MySQL setup

This phase prepares one hosted MySQL 8.4 database shared by the existing admin and scanner applications. It does not deploy either application, enable email delivery, or require your own AWS account. The existing database adapter supports a remote hostname, a provider-specific port, and verified TLS.

## 1. Create the service

1. Open [Aiven Console](https://console.aiven.io/) and create an account or sign in.
2. Open a project, select **Services**, then **Create service**, then **MySQL**.
3. For an initial development exercise, consider the **Free** tier if it offers MySQL 8.4. Review the displayed plan and cost before creating anything. Do not select a paid plan automatically. Free services have capacity and availability limits and are not evidence of production suitability.
4. Use a service name such as `meal-management-dev`. Select MySQL **8.4** explicitly wherever version selection is available. If the console does not expose the version, confirm the resulting service version before connecting or moving data; do not assume the default is 8.4.
5. Wait until the service reports it is running. Record the service name and version. Keep its password private.

The cloud-provider selection is Aiven's hosting infrastructure; it does not require supplying personal AWS credentials. Free-tier region selection may be unavailable. Review region, data residency, capacity, backups, and availability before using employee data.

Official references: [service creation](https://aiven.io/docs/products/mysql/get-started), [version selection](https://aiven.io/docs/products/mysql/howto/manage-mysql-version), and [free-tier limits](https://aiven.io/docs/products/mysql/concepts/mysql-free-tier).

## 2. Prepare a separate connection configuration

After the service exists, use its Overview or Quick connect panel to obtain the hostname, port, username, and CA certificate. Save the certificate privately, for example at `var/private/certificates/aiven-ca.pem`. Keep passwords and connection URLs out of chat, screenshots, source code, and shell history.

Keep the working `.env` unchanged during preparation. When ready, make a private `.env.aiven` copy using the editor. `.env.aiven` and `var/private/` are already excluded by `.gitignore`. Preserve `QR_ENCRYPTION_KEYS` exactly when moving existing records; newly generated keys cannot decrypt existing employee QRs.

Update these settings in the separate file:

| Setting | Value |
| --- | --- |
| `DB_HOST` | Exact Aiven hostname; not localhost or a numeric IP |
| `DB_PORT` | Exact port shown by Aiven; do not assume 3306 |
| `DB_NAME` | Existing target database name confirmed before access |
| `DB_USER` | Approved database account |
| `DB_PASSWORD` | That account's password, stored privately |
| `DB_SSL_CA` | Absolute path to the downloaded CA certificate |
| `EMAIL_BACKEND` | `preview` during database validation |
| `EMAIL_SEND_ENABLED` | `false` |
| `EMAIL_AUTO_SEND_ENABLED` | `false` |

The provided CA enables both certificate and hostname verification in the existing adapter. Never disable these checks to resolve a connection error. Restrict database network access to approved clients; review any IP-filter change before applying it.

For local application checks against the hosted database, keep development mode and loopback browser origins. This is not public deployment configuration. Production storage and browser security remain a later phase.

Run offline validation from a new terminal in the project root after filling in the private file:

```sh
.venv/bin/python manage.py --env-file .env.aiven check-config
.venv/bin/python manage.py migration-plan
```

These commands do not open a database connection. The environment loader preserves already-exported variables, so avoid stale exported database settings in that terminal. Configuration validation does not prove certificate validity, connectivity, account permissions, server version, or schema readiness. The migration plan lists repository migrations, not remotely pending migrations.

## 3. Confirm access before connecting

Confirm the exact service hostname, port, database name, and account before the agent makes the first connection. Passwords stay in private configuration or an interactive password prompt. Read-only schema inspection requires separate approval and uses the existing `schema-inspect` command; it reads metadata and temporarily takes an advisory lock.

The target database must already exist. Aiven may provide `defaultdb`; decide whether to use it or create `meal_management` after reviewing the service. The migration runner does not create databases. Use a separate restricted runtime account for both applications and an approved migration account for schema work. Aiven's provider administrator is not the laptop's MySQL root account. Verify runtime grants and migration permissions, including triggers and advisory locks, on the selected service.

## 4. Preserve existing data before switching

Use the [guarded local backup and Aiven restore workflow](aiven-transfer.md) for the existing application. It separates the operator's secure local backup from the approved empty-target restore and application switch.

Choose the transfer procedure after reviewing both schemas. Do not initialize tables in a destination intended for a full schema-and-data restore. The repository currently contains migrations 001 through 006; actual pending migrations require live inspection.

For the existing application, plan an approved local backup and restore into an empty destination. Preserve employee, staff, QR, scanner configuration, meal, audit, email-queue, and migration-ledger records. Review trigger definitions and managed-service restore privileges before import. Preserve all applicable QR encryption keys and separately retain private photo files, which are not stored in MySQL. Avoid exposing the laptop database to the internet for migration.

Before final transfer, arrange a pause in application writes, complete a consistent backup, restore it, and verify the result. Confirm scanner idempotency, employee and visitor reports, and both applications' use of the same target. Review queued email state while sending remains disabled. Changing the database hostname alone does not transfer any records.

After verification and approval, update both applications together and restart them in a planned switch. Reconcile any writes made to the new database before attempting a rollback. Use a separate disposable database for integration tests; never point destructive integration tests at the shared application database.

No connection, database creation, migration, import, account change, or application switch is performed by this guide.
