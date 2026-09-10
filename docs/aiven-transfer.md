# Transfer the local database to Aiven

This workflow prepares a consistent local backup, restores it into an empty Aiven MySQL database, and verifies the result before either application switches databases. The applications continue running on the laptop after the switch; this does not publicly deploy them. Real email delivery remains disabled during transfer and verification.

## Current transfer status, 2026-09-10

The completed backup `var/private/backups/20260910T065506Z-0f524f477517d625` was restored into Aiven MySQL 8.4.8 `defaultdb` over verified TLS. The restored schema, records, and triggers matched the backup before migration 006 was applied. Migrations 001–006 are now applied on Aiven. Do not rerun the empty-target restore command against this populated database.

Final verification passed with 4 employees, 5 QR credentials, 13 email-history records, 0 meals, 25 tables, and 28 triggers. All original local table digests still match the backup. Existing QR credentials decrypt with the preserved keys, and all 4 private photos remain unchanged on the laptop. Email history contains 10 cancelled messages, 1 previously sent message, and 2 legacy messages now awaiting approval. No messages were sent during transfer.

The first post-migration verification stopped because migration 006 automatically advanced `email_queue.updated_at` for the two queued messages whose status changed. The corrected verifier permits only those expected timestamp changes within the migration's recorded execution window. A subsequent read-only verification passed without repeating the import or migration. The private verification report is `var/private/backups/20260910T065506Z-0f524f477517d625.restore-20260910T070213646618Z.json`.

The application cutover is pending. `.env` still selects the local database; `.env.aiven` still contains the migration account and has preview email selected with both sending flags disabled. No runtime account was created and neither application was restarted. Automatic approval review blocked the proposed account creation and configuration switch because its host scope had not been explicitly approved. The proposed scope is recorded below for review.

The remaining numbered sections describe the repeatable procedure. Backup and restore steps are already complete for the transfer above. Full application writes, concurrent MySQL integration tests, physical camera checks, and real email delivery remain unverified for this cutover.

## 1. Keep the configurations separate

Keep the working local `.env` unchanged. Prepare `.env.aiven` privately using the [Aiven setup instructions](aiven-setup.md). Preserve the source QR encryption keys, browser signing and rate-limit secrets, local browser origins, scanner configuration, and the local photo directory. Changing encryption keys would prevent recovery of existing QR credentials. Changing scanner cookie settings or signing secrets can interfere with pending serving recovery.

Use these settings for this laptop-to-hosted-database exercise:

- `APP_ENV=development` and `PHOTO_BACKEND=local` in both selected files.
- Aiven's exact hostname, port, existing database name, approved migration account, and absolute CA certificate path in `.env.aiven`.
- `EMAIL_BACKEND=preview`, `EMAIL_SEND_ENABLED=false`, and `EMAIL_AUTO_SEND_ENABLED=false` in `.env.aiven`.
- The existing loopback browser origins and private local photo root in `.env.aiven`.

The new `backup-local` and `restore-aiven` commands require an explicit `--env-file`. They read that file without environment interpolation and do not inherit exported database settings or modify the process environment. This differs from the older general-purpose CLI commands, whose dotenv loader preserves existing exported variables.

Both MySQL client tools must already be available through `PATH`: the backup helper requires `mysqldump` 8.4, and restore uses `mysql`. Install missing dependencies only after separate approval. The migration runner does not create the target database. Do not initialize tables in the empty destination before this full schema-and-data restore.

The export uses a logical SQL backup and includes triggers, which require the corresponding MySQL privilege. See the [MySQL mysqldump reference](https://dev.mysql.com/doc/refman/8.4/en/mysqldump.html) and [Aiven backup and restore documentation](https://aiven.io/docs/products/mysql/howto/migrate-database-mysqldump).

## 2. Pause every writer

Finish or reconcile pending scanner servings. Stop the administrator and scanner processes in their own terminals, then stop any separate email worker, CLI sender, import, script, or other process that writes to the local database or photo directory. Keep them stopped throughout backup, restore, and comparison. Do not delete scanner cookies or clear pending browser storage.

The `--writers-paused` flag records the operator's confirmation; it does not stop processes itself. Backup compares the source before and after export and refuses completion if the recorded database, configuration, or photos change. That check does not replace the writer pause. The source database remains available and is not migrated or overwritten by the transfer helpers.

## 3. Create the local backup in your terminal

After approving local database access and confirming the writer pause, run this from the project folder:

```sh
.venv/bin/python manage.py --env-file .env backup-local --allow-database-access --writers-paused
```

The helper asks you to type `BACKUP` followed by the exact displayed local `host:port/database`, then securely prompts for the laptop's **MySQL root password**. This is not your application administrator password and not the Aiven password. No password appears on the command line or in normal output. The helper refuses a noninteractive terminal or a password prompt that would echo its input.

The root password is used for the backup operation. A temporary MySQL client options file uses restrictive permissions and is removed during cleanup; the completed backup does not retain that temporary root credential. The backup does contain the selected application environment file, including its configured secrets, so treat the entire backup as confidential.

A successful command reports a unique directory under `var/private/backups/`. It contains the original SQL dump, a portable dump, trigger definitions, the selected configuration, private photos, a manifest, and a completion marker. File checksums and private permissions are verified before success is reported. This confirms backup consistency checks; it does not prove that restore has succeeded.

Keep the complete directory intact. Give the agent the directory path and completion outcome when handing off the next step. Do not paste SQL, upload the environment file, or share database passwords, QR payloads, photos, or the full backup in chat. If backup fails, preserve its partial directory for local review; do not use it as a completed backup.

Unexpected backup failures now report the operation stage and, for connector errors, the numeric MySQL error code. A private `failure.json` contains only these diagnostic labels and no exception text, credentials, SQL, or employee values. MySQL error 1045 means the login was rejected; confirm the local root password and account access. The original generic error cannot establish that a password was wrong. A failed directory remains incomplete and cannot be restored. A new backup attempt creates a separate directory and preserves earlier attempts.

An export failure with a zero-byte `original.sql` was traced to the unsupported `connect-timeout` option in the generated mysqldump configuration. The backup no longer writes that option; the overall export process retains its timeout. The unit suite now validates both generated export commands and the private options file using the installed client's `--help` mode without connecting to MySQL. This parser check does not execute a live backup.

A later `TRIGGER_METADATA` failure was caused by the connector returning the trigger SQL_MODE field as a Python set. The snapshot now reads that field as text and normalizes its flags into a stable comma-separated string compatible with restoration. The complete snapshot and trigger metadata must serialize successfully before either SQL export starts. Existing incomplete export directories remain preserved and cannot be restored without a valid completion marker.

## 4. Approve and perform the empty-target restore

Review the completed backup and reconfirm the exact Aiven hostname, port, and database before authorizing remote database changes. The command below is a template: replace the backup path and `HOST:PORT/defaultdb` with the reviewed values.

```sh
.venv/bin/python manage.py --env-file .env.aiven restore-aiven --backup /absolute/path/to/completed-backup --expected-target HOST:PORT/defaultdb --allow-database-changes --writers-paused
```

Restore independently checks the target identity, verified TLS configuration, local development settings, disabled sending, backup completion, matching secrets, and the unchanged local photos. It refuses a destination with existing application objects. It does not drop tables, create a replacement database, or clean up a failed import automatically.

The helper imports the source schema and records, recreates triggers for the managed database, and compares restored data against the backup. It accepts source migration ledgers through 005 or 006. When the source ends at 005, it applies pending migration 006 after import; it refuses an unexpected migration sequence. Migration 006 preserves employees, QRs, meals, and terminal email outcomes, while older queued messages become legacy messages awaiting explicit approval.

Successful return requires the helper's row, trigger, migration-ledger, email-transition, and QR-decryption checks to pass. The safe verification report lists counts and migration outcomes without QR tokens or passwords. Review that report before proceeding. These checks do not replace the separate MySQL concurrency and full application integration suite.

MySQL DDL is not an all-or-nothing application transaction. A failed import or migration can leave partial target objects. Preserve the backup and failure outcome, keep applications pointed away from that target, and inspect before deciding on a separately approved repair. Do not blindly rerun restore or delete the partial target to make the command pass. The original local database is unchanged by this restore process.

## 5. Prepare the runtime account and switch both applications

Provision and verify a separate restricted Aiven runtime database account before starting either application against Aiven. Account creation and grants require their own approved database changes. Replace the migration-owner credentials in `.env.aiven` with the runtime account only after restore verification. Do not run the applications under the migration account or Aiven's provider administrator account.

### Pending account and configuration proposal

Create a new `meal_runtime` account with a securely generated password and mandatory SSL. The proposed MySQL host is `%`, which allows the account to authenticate from any source host that can reach the existing Aiven service. It is not an IP restriction. This does not change Aiven firewall settings or expose the application servers; both HTTP servers remain bound to laptop loopback. Approval of this account scope is still required.

Grant `SELECT` on `defaultdb.*` and only the following table-specific write privileges for the current HTTP applications:

| Privilege | Tables |
| --- | --- |
| `INSERT` | `departments`, `employees`, `staff_accounts`, `staff_account_roles`, `staff_sessions`, `locations`, `scanner_devices`, `meal_types`, `qr_credentials`, `email_queue`, `audit_events`, `serving_requests`, `visitor_authorizations`, `servings`, `meals`, `scan_attempts`, `scan_app_requests`, `employee_email_batches`, `login_rate_limits` |
| `UPDATE` | `employees`, `staff_accounts`, `staff_sessions`, `locations`, `scanner_devices`, `qr_credentials`, `email_queue`, `serving_requests`, `visitor_authorizations`, `scan_attempts`, `login_rate_limits`, `scan_app_requests`, `employee_email_batches`, `system_locks` |

No `DELETE`, schema changes, trigger creation, account management, or grant option is proposed. The migration ledger, seed registry, roles, and scanner settings remain read-only for this account. `UPDATE` on the immutable request/batch and system-lock tables supports existing locking reads; their database guards continue to apply. Staff role replacement and meal-type activation services that are not exposed by current HTTP routes would require separately added privileges if exposed later. Keep the migration account as the existing trigger definer.

Before switching, preserve the original local configuration privately as `.env.local` and the migration configuration as `.env.aiven.migration`, refusing to overwrite existing files. Store the generated runtime credentials only in private files with mode `0600`. Verify the account's exact grants, TLS connection, migration ledger, and existing scanner profile before updating `.env` and `.env.aiven` together. Retain the existing encryption keys, photos, browser secrets, and loopback origins. Keep `EMAIL_BACKEND=preview`, `EMAIL_SEND_ENABLED=false`, and `EMAIL_AUTO_SEND_ENABLED=false`.

The proposed account and configuration changes have not run. After approval and successful verification, start each application independently and check its live and ready endpoints before resuming normal use.

Keep the same private local photo directory and all applicable encryption keys. Photo files remain on the laptop for this phase; moving MySQL does not upload photos to Aiven. Retain the backup copies separately.

After the configuration switch is approved, start the administrator and scanner in separate terminals:

```sh
sh deploy/start-admin.sh --env-file .env.aiven --env-file-only
```

```sh
sh deploy/start-scanner.sh --env-file .env.aiven --env-file-only
```

The file-only flag prevents inherited environment variables from overriding the selected file. The administrator remains at `http://localhost:8000/` and the scanner at `http://localhost:8001/`. Both must use the same reviewed Aiven database. Starting one application does not start the other or apply migrations.

Check each application's `/health/live` first. The separate `/health/ready` checks connect to MySQL and require approved database access; both must report ready with migrations 001–006 applied. Review employee and QR records, local photo retrieval, existing reports, and email approval states. Use separately approved test data for scan writes and deliberate retry checks. Keep real email disabled until its own verification and approval.

## Vercel-only runtime-account proposal

The current next step targets the two prepared Vercel projects. It does not authorize the earlier laptop configuration replacement or restart instructions. On the existing Aiven service `mysql-a0846c8-mcciaexplore-0d76.a.aivencloud.com:27404/defaultdb`, create `meal_runtime`@`%` only if absent, with a securely generated password and `REQUIRE SSL`. Apply exactly the `SELECT`, table-specific `INSERT`, and table-specific `UPDATE` grants listed above. Refuse an existing account rather than resetting its password or adding privileges to it. The `%` scope allows authentication from any source host that can reach the existing Aiven service; mandatory TLS and a password still apply.

Verify the new account's exact grants, verified TLS connection, and migration ledger using read-only checks. Save the connection settings and generated password only in a new `var/private/vercel/database-runtime.env` file with mode `0600`, refusing to overwrite an existing file and using a private parent directory. This path is ignored by Git. Preserve `.env`, `.env.aiven`, existing QR keys, photos, database objects, records, and local application processes. No migrations, existing-row changes, deployment, email sending, firewall changes, or cloud environment updates are included in this account-only proposal.

The user chose to execute this step manually. The [terminal command and recovery instructions](aiven-runtime-account.md) implement this proposal with an explicit change flag and typed target confirmation. The account has not been created by the agent. Successful grants and read-only checks will not establish that application writes, triggers, concurrent requests, email providers, or hosted runtime behavior work under the new account; those checks remain separate.

## 6. Preserve a recovery point

Retain the untouched local database and completed backup through the cutover review. Once the Aiven-backed applications accept new writes, switching back to the old local database would omit those new records. Pause both applications and reconcile the databases before any rollback. Keep schema changes, credential rotation, email sending, public application deployment, and backup-retention decisions as separately reviewed steps.
