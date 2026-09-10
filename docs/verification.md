# Verification status

## Manual Aiven runtime-account command prepared, 2026-09-10

Added `manage.py --env-file .env.aiven create-aiven-runtime --allow-database-changes` for the user's manual execution. It previews the exact account and grants, requires an interactive exact-target confirmation, uses only the selected file, creates a new account with mandatory SSL, and verifies its own grants, identity, roles, TLS, and migration ledger before reporting success. It synchronizes an exclusive owner-only pending credentials file before account creation and publishes the final filename only after verification. Existing account creation failures, ambiguous responses, partial grants, and verification failures stop without retries or account deletion. Neither existing environment files nor application records are modified by this workflow.

All 881 Python unit/API tests passed with no skips, including 28 new helper tests and 13 new CLI tests. The real filesystem checks used temporary fictional credentials; database calls were mocked. Both existing Vercel package checksum checks and CLI help passed. Packaging tests also verify that the new account-management module and private credential files are excluded from uploads. The full frontend suite and the separate 97 MySQL integration tests were not rerun. The installed Starlette TestClient continues to emit its existing HTTPX deprecation warning.

No real database connection, account creation, credential-file generation, Vercel upload/deployment, storage operation, dependency installation, or email sending occurred during this implementation. Successful live account creation and grant verification remain pending until the user executes the [manual command](aiven-runtime-account.md).

## Both private storage connections verified, 2026-09-10

Read-only inspection of the existing Blob store's Connections page now confirms both `mccia-meal-admin` and `mccia-meal-scanner`, each connected for Production with `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_ID`, and `BLOB_WEBHOOK_PUBLIC_KEY`. The store remains Private with 0 B. No token values were read and no files were uploaded. The dashboard's optional token-revocation recommendation must not be applied to the current code, which still explicitly requires the read/write token. The restricted Aiven account and remaining hosted settings are pending; the Vercel-only account proposal is recorded separately from the older laptop cutover procedure in `docs/aiven-transfer.md`. No tests were rerun for this read-only verification and documentation update.

## Admin storage connected; scanner connection pending, 2026-09-10

The Blob store's Connections page confirms `mccia-meal-admin` connected for Production with `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_ID`, and `BLOB_WEBHOOK_PUBLIC_KEY`. The scanner was not listed, despite the user's general completion message. Its connection form is now prepared with Production only, Sensitive enabled, the read/write token option enabled, and an empty custom prefix. The final Connect Project action was handed to the user and remains unverified. No token values were retrieved, and the store still reports 0 B. No database, application deployment, or photo transfer actions occurred. The required restricted Aiven runtime account and the remaining application environment settings are still pending.

## User-created Vercel projects, 2026-09-10

Read-only authenticated CLI checks confirmed account `mcciaexplore-netizen` and the newly created `mccia-meal-admin` and `mccia-meal-scanner` projects in `mccias-projects`. Neither has a production URL. The existing private Blob store remains empty. Its connection form was prepared for the admin project with Production only, Sensitive enabled, no custom variable prefix, and the read/write token option enabled; the final Connect Project action was handed to the user and has not been verified. The scanner connection also remains pending. No application upload, deployment, token retrieval, private configuration change, photo transfer, database connection, or real email occurred. No tests were rerun for these read-only checks and documentation updates.

## User-installed Vercel tools, 2026-09-10

The user's terminal output confirms Vercel CLI 59.15.1 installed under `build/vercel-tools` and Python SDK 0.10.0 installed in `.venv`. The CLI version command succeeds. An npm engine warning identifies a transitive dependency whose supported range excludes the installed Node 25.6.1; this is not evidence that a full deployment will succeed. Python's dependency check reports no broken requirements. Read-only inspection of the installed SDK confirms compatibility with the current private storage adapter's constructor, methods, error class, and result attributes.

All 51 Vercel unit tests passed with no skips using the documented `PYTHONPATH=backend` setting; an initial invocation without that setting could not import two test modules and was corrected. Both existing admin and scanner package checksum verifiers passed. The full Python and frontend suites and the separate MySQL integration suite were not rerun. No provider requests, database connections, real emails, photo uploads, credentials changes, CLI login, or application deployments were performed during these checks. Historical installation-pending statements below describe earlier stages.

## Private photo store created by the user, 2026-09-10

The user clicked Create in Vercel. Read-only dashboard verification confirmed `mccia-meal-photos` under `MCCIA's projects`, with Private access, Mumbai/BOM1 region, and 0 B stored. No SDK operation, upload, token retrieval, project connection, or application deployment was performed during this verification. The team currently displays Hobby. The Vercel CLI is not available on the checked shell PATH and the project's virtual environment does not have the newly declared `vercel` SDK installed. Dependency installation remains a separate approval step. Existing tests were not rerun for this documentation-only update.

## Vercel deployment preparation, 2026-09-10

Vercel is the selected application host. Offline preparation adds separate fixed-role upload packages, private Vercel Blob photo storage, production configuration validation, Aiven CA materialization, platform HTTPS/client identity handling, and a scanner API allowlist for approved office public networks. Private environment files, SQL backups, photos, and database-management tools are excluded from the generated deployment packages. Neither Vercel project has been published.

The Vercel runtime rejects the provider administrator database account, background email polling, photo limits above 4,000,000 bytes, and manual bulk processing limits other than one message per request. Existing direct-send and bulk-approval behavior is preserved. Frontend upload validation and displayed limits follow the server configuration. The local defaults remain 5 MiB per photo and up to ten messages per processing request.

All 840 Python unit/API tests and all 180 frontend tests passed. Coverage includes private Blob URL and host validation, bounded private reads, credential redaction, fixed project roles, app-route isolation, missing/invalid certificate and production settings, scanner network restrictions, spoofed or missing proxy headers, safe package contents, per-request email batches, and upload limits. These use injected services and mocked storage, not real employee writes or cloud provider calls. The separate 97 MySQL integration tests remain unverified and were not rerun in this preparation.

Both actual deployment packages were generated and checksum-verified: `build/vercel/admin` contains 48 files totaling 513,648 bytes, and `build/vercel/scanner` contains 50 files totaling 676,156 bytes. All 34 copied runtime source files in each package matched the prepared source. Separate-process TestClient checks loaded each generated entrypoint with fictional settings: root and liveness returned 200, each application's own JavaScript returned 200, the other application's JavaScript returned 404, employee administration required authentication on admin and returned 404 on scanner, and scanner session access existed only on scanner. Private environment, backend source, and photo paths returned 404. Database/provider calls remained zero and the Blob SDK was not imported. These checks ran with the installed Python 3.14; the packages select Vercel Python 3.12, whose hosted build and runtime have not been executed. Vercel configuration was checked against published documentation; no full JSON Schema validator was installed or run. Git whitespace checks passed.

No dependencies were installed, no real email was sent, and no database connection, migration, photo transfer, AWS access, Vercel resource creation, or public network exposure was performed during this phase. The new Blob SDK dependency is declared but not installed or verified against a live store. Hosted build/runtime checks, office-network checks, real Gmail delivery, phone cameras, and the existing-photo transfer remain pending. Application publication still requires the reviewed provider account/configuration, a private Blob store, and the previously blocked restricted Aiven runtime account. See [the Vercel deployment procedure](vercel-deployment.md).

## Verified Aiven restore, 2026-09-10

Completed backup `20260910T065506Z-0f524f477517d625` was restored to the approved Aiven MySQL 8.4.8 `defaultdb` over verified TLS 1.3. The exact schema, trigger, and table-data comparison passed before migration 006, which is now recorded as APPLIED. Every local source table still matches its backup hash.

The initial post-migration verifier rejected the two expected `updated_at` changes caused by holding queued emails. The verifier now checks those timestamps against migration 006's recorded execution window while requiring terminal-email timestamps and all other preserved fields to remain unchanged. Read-only verification then passed; neither the restore nor the migration was rerun.

The verified target has 25 tables, 28 triggers, 4 employees, 5 QR credentials, 13 email records, and no meals. Email states are 10 CANCELLED, 1 SENT, and 2 PENDING_APPROVAL; all retained rows are LEGACY with null approval fields. Five QR decryption checks and four local photo checks passed. The private result is recorded in [the restore verification report](../var/private/backups/20260910T065506Z-0f524f477517d625.restore-20260910T070213646618Z.json).

All 781 Python unit/API tests passed, including 24 transfer tests. The 97 MySQL integration tests remain previously skipped and unverified. No new frontend, device, or live-email verification was performed.

Runtime-account creation and the application configuration switch remain blocked: automatic approval review rejected the proposed wildcard MySQL host scope (`'%'`) because that exact scope had not been explicitly authorized. No runtime account was created, no private environment files were changed, and neither application was restarted. The revised grant plan in [the transfer procedure](aiven-transfer.md) requires approval before these steps proceed.

The preparation and email-workflow sections below are historical records. Their pending-work statements describe the state at those earlier stages; the restore outcome above and remaining checks at the end are current.

## Trigger metadata serialization correction, 2026-09-10

The next user-run backup completed both SQL exports but failed while writing trigger metadata, leaving no completed backup. A read-only system-column check confirmed that information_schema.TRIGGERS.SQL_MODE is a SET; the installed connector converts that type into a Python set, which the metadata JSON serializer could not encode. The snapshot query now casts SQL_MODE to text and normalizes mode ordering. Restore comparison treats equivalent mode ordering consistently while preserving the mode flags and trigger definitions.

A zero-row read-only query on the local MySQL server confirmed that the original field has the SET protocol flag and the cast field does not. No trigger contents were retrieved by that check. Regression tests reproduce conversion with the installed connector, serialize a complete snapshot, validate empty and reordered modes, reject malformed modes, ensure invalid metadata stops before exports, and check the restore parameter format. All 775 Python unit/API tests passed, including 37 backup tests and 18 restore tests. A successful complete live backup and Aiven restore remain pending; no source records or target database objects were changed during this fix.

## Export option correction, 2026-09-10

A later user-run attempt passed the source snapshot and failed during export, leaving a zero-byte `original.sql`. Offline reproduction with fictional credentials confirmed that the installed mysqldump 8.4.11 rejects `connect-timeout=10` in its options file with exit code 7. The backup configuration no longer includes that option. Both complete export argument sets and the generated private options file now pass the installed client's `--help` parser without opening a database connection.

The full Python unit/API suite passed 770 tests with no skips after this correction, including 33 backup tests. The new client test skips explicitly on systems without mysqldump; it ran successfully here. No live backup retry, database changes, or Aiven transfer was performed during this fix. Successful export and restore still require the user to rerun the privately prompted backup command; MySQL integration tests remain unverified.

## Backup failure diagnostics, 2026-09-10

The first user-run backup attempt returned the generic partial-directory error and left an empty incomplete directory. It did not retain the underlying exception, so its exact cause is unknown. A subsequent approved read-only probe using the existing local application account verified connection, a consistent read-only transaction, and the backup's initial metadata queries. It did not use or verify the local root password, retry the backup, or change either database.

Unexpected failures now report a fixed operation stage and validated numeric MySQL error code where available. Private failure diagnostics exclude exception text, SQL, credentials, and employee values. Cursor and connection cleanup preserve an original failure rather than replacing it with a cleanup error. All 769 Python unit/API tests passed after this change, including 32 backup tests and 16 transfer CLI tests. MySQL integration tests were not rerun; actual successful backup, import, migration 006 execution, and cutover remain unverified and pending.

## Aiven transfer preparation, 2026-09-10

Approved read-only connections verified Aiven MySQL 8.4.8 with TLS 1.3, certificate and hostname verification, and UTC session time. The selected `defaultdb` has no tables, triggers, routines, or events and uses utf8mb4 with utf8mb4_0900_ai_ci. The configured migration account has the relevant table and trigger privileges; actual import execution has not been tested.

The local MySQL 8.4.11 `meal_management` database has 24 InnoDB tables, 4 employees, 5 QR credentials, 13 email-history rows, and no meals. Migration ledger entries 001–005 match the repository checksums; migration 006 is pending. The local application account lacks TRIGGER privilege, so its empty trigger listing cannot establish whether source triggers are present. The full backup must use the privately prompted local MySQL administrator and verify trigger visibility. The configured QR encryption keys match the Aiven settings, and email sending is disabled in the Aiven configuration.

A complete backup, restore, migration execution, restricted runtime account creation, and application cutover have not been performed. The workflow needs a successful privately prompted backup in the user's Terminal. The original `.env`, existing database records, and running-process configuration have not been changed by this preparation.

The full Python unit/API suite passed 761 tests with no skips after this update. This includes 25 backup tests, 17 restore tests, 15 transfer CLI tests, and 11 launcher tests. Coverage includes private credential cleanup, incomplete backups, row digests, expected trigger visibility, destination and permission guards, empty-target refusal, migration/email preservation checks, decryption checks, and file-only startup configuration. Database operations and client imports are mocked in these tests; a real backup-and-restore round trip remains unverified.

The separate MySQL integration discovery skipped all 97 tests with both opt-in flags explicitly disabled. The installed MySQL and mysqldump clients report version 8.4.11, and the new CLI help and launcher help were checked without database access. Frontend files were not changed and frontend tests were not rerun in this phase. See [the transfer procedure](aiven-transfer.md) for the next Terminal action.

## Earlier email workflow verification

The employee email workflow was verified locally on 2026-09-10. Individual registration now creates a single draft for an explicit Send email action. Bulk import creates pending approvals; Approve and send commits the administrator's approval before processing that exact selection. This source update did not connect to a database, send messages, restart the live applications, install dependencies, access AWS, or change private configuration.

| Check | Result |
| --- | --- |
| Python unit and API suite | 702 passed, no skips |
| Frontend Node suite | 174 passed, no failures or skips |
| Frontend asset build | Passed for separate admin and scanner bundles |
| Built assets through injected FastAPI applications | Both root pages and their own JavaScript served; each rejected the other application's JavaScript with 404; database calls remained unused |
| Migration plan | Six migration definitions loaded and checksummed offline; original migrations 001–005 retained their checksums |
| Source policy | Included in the Python suite; authored Python has no comments or docstrings |
| MySQL integration discovery | 97 tests skipped with both database opt-in flags explicitly disabled; includes 12 new policy scenarios and 13 delivery scenarios |
| Live email verification for this workflow | Not performed |
| Physical browser/camera/device verification | Not performed in this phase |

## Earlier email workflow coverage and limits

The new tests cover administrator authentication, server role enforcement, CSRF, private status recovery, single-send employee/email binding, disabled provider configuration, unchanged retry identifiers, and safe transaction errors. Scanner routes expose no employee import, approval, sending, or email-status APIs. API tests use injected services; they do not prove MySQL locking behavior.

Service tests cover single DRAFT creation, bulk PENDING_APPROVAL creation, approval attribution, atomic rollback, normalized payload binding, bulk batch filtering, resend draft reuse, and safe classification of uncertain claims. Delivery tests ensure only approved messages can reach a provider and that the dispatcher excludes every SINGLE row. Single sends can resume the same QUEUED ID after approval commits but delivery has not begun. Sent results replay without a provider call; failed and uncertain attempts are never automatically retransmitted. Approved messages whose QR subsequently expires reach the worker's validation and cancellation path rather than blocking processing of the entire selection during preflight.

Frontend tests cover CSV parsing and preview, bounded batch sizes, exact-ID approval response validation, one-click approval followed by processing, no processing after failed or uncertain approval, serial processing in groups of at most ten, and interruption recovery. Import markers store only the authenticated caller's request UUID, payload fingerprint, and row count. Repeating an uncertain import requires the original CSV and the same UUID; a known fresh validation rejection permits correction. Tests also cover single Send email and Resume send behavior, status checks after lost responses, private preview eligibility, and existing registration camera cleanup. Simulated DOM and media objects do not establish physical camera behavior.

Existing suites continue to cover both EMPLOYEE and MASTER QRs, committed meal replay, visitor-field validation, reports, password verification, sessions, input validation, private photos, and scanner isolation. Frontend builds and injected HTTP asset checks establish packaging and route separation; this phase did not restart the actual local services or verify meal recording against MySQL.

The new migration preserves every existing email record, marks existing rows LEGACY, and holds previously QUEUED rows for approval. New constraints require an approval stamp before QUEUED status, and triggers preserve attribution. Offline parsing and assertions are not execution of this SQL. The new MySQL tests cover real concurrent bulk retries and approvals, foreign keys/checks/triggers, rollback, unchanged QR reuse, and upgrading historical queue rows, but they remain unexecuted.

An earlier separately approved Gmail test was accepted by the provider and recorded as SENT in the local database. That historical result does not verify the new browser workflow, current credentials, inbox arrival, MySQL concurrency, or failure recovery. Earlier independent-process checks used temporary loopback listeners with database calls blocked. These historical results are not counted as new end-to-end verification.

The installed Starlette TestClient reports an HTTPX deprecation warning. Tests pass with the existing dependencies; no new test-client package was installed.

## Required activation and remaining checks

- Approve the exact restricted runtime-account host scope and grants in [the transfer procedure](aiven-transfer.md), then create and verify that account. Automatic approval review currently blocks that account change and the dependent configuration switch.
- After account verification, switch both applications to the same verified Aiven target and coordinate their restarts. Check both readiness endpoints, authenticated reports, and scanner behavior while email sending remains disabled. No application cutover has occurred yet.
- Retain the completed backup, restore report, existing QR keys, local photos, and unchanged local source database. Aiven already contains the verified restored records and migration 006; do not rerun the restore or migration to resolve the earlier verifier failure. Reconcile any new Aiven writes before a future rollback.
- Run the 97 MySQL integration tests only against a separately approved disposable database namespace. Never use the application database for destructive integration tests. Exercise both fresh schema creation and upgrade from a backed-up existing schema.
- Enable `EMAIL_SEND_ENABLED` only after approval. Leave `EMAIL_AUTO_SEND_ENABLED=false` for direct single sends and browser-driven approved bulk processing. Optional automatic recovery can later be enabled for approved bulk/legacy messages only.
- Verify a reviewed single recipient and a reviewed bulk batch with explicit real-email authorization. Check provider failures, lost responses, pending approvals, database commit recovery, and dashboard status. Do not infer inbox delivery from SMTP acceptance.
- Verify the dashboard and scanner on actual laptop and phone browsers, including photo capture and QR scanning. Network exposure and HTTPS changes still need separate approval.
- Complete private production storage, HTTPS/proxy trust, ongoing backup and recovery arrangements, monitoring, delivery/bounce handling, and deployment verification before claiming production readiness. The completed Aiven transfer establishes this restore's result; it does not verify those operational requirements. AWS access and resources remain optional later work.

See [email workflow](email-delivery-workflow.md), [Gmail setup](gmail-setup.md), [Aiven setup](aiven-setup.md), [API contracts](api.md), and [deployment instructions](deployment.md). Skipped integration tests and unavailable manual checks are not counted as passes.
