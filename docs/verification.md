# Verification status

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

## Coverage and limits

The new tests cover administrator authentication, server role enforcement, CSRF, private status recovery, single-send employee/email binding, disabled provider configuration, unchanged retry identifiers, and safe transaction errors. Scanner routes expose no employee import, approval, sending, or email-status APIs. API tests use injected services; they do not prove MySQL locking behavior.

Service tests cover single DRAFT creation, bulk PENDING_APPROVAL creation, approval attribution, atomic rollback, normalized payload binding, bulk batch filtering, resend draft reuse, and safe classification of uncertain claims. Delivery tests ensure only approved messages can reach a provider and that the dispatcher excludes every SINGLE row. Single sends can resume the same QUEUED ID after approval commits but delivery has not begun. Sent results replay without a provider call; failed and uncertain attempts are never automatically retransmitted. Approved messages whose QR subsequently expires reach the worker's validation and cancellation path rather than blocking processing of the entire selection during preflight.

Frontend tests cover CSV parsing and preview, bounded batch sizes, exact-ID approval response validation, one-click approval followed by processing, no processing after failed or uncertain approval, serial processing in groups of at most ten, and interruption recovery. Import markers store only the authenticated caller's request UUID, payload fingerprint, and row count. Repeating an uncertain import requires the original CSV and the same UUID; a known fresh validation rejection permits correction. Tests also cover single Send email and Resume send behavior, status checks after lost responses, private preview eligibility, and existing registration camera cleanup. Simulated DOM and media objects do not establish physical camera behavior.

Existing suites continue to cover both EMPLOYEE and MASTER QRs, committed meal replay, visitor-field validation, reports, password verification, sessions, input validation, private photos, and scanner isolation. Frontend builds and injected HTTP asset checks establish packaging and route separation; this phase did not restart the actual local services or verify meal recording against MySQL.

The new migration preserves every existing email record, marks existing rows LEGACY, and holds previously QUEUED rows for approval. New constraints require an approval stamp before QUEUED status, and triggers preserve attribution. Offline parsing and assertions are not execution of this SQL. The new MySQL tests cover real concurrent bulk retries and approvals, foreign keys/checks/triggers, rollback, unchanged QR reuse, and upgrading historical queue rows, but they remain unexecuted.

An earlier separately approved Gmail test was accepted by the provider and recorded as SENT in the local database. That historical result does not verify the new browser workflow, current credentials, inbox arrival, MySQL concurrency, or failure recovery. Earlier independent-process checks used temporary loopback listeners with database calls blocked. These historical results are not counted as new end-to-end verification.

The installed Starlette TestClient reports an HTTPX deprecation warning. Tests pass with the existing dependencies; no new test-client package was installed.

## Required activation and remaining checks

- Confirm the exact target before schema inspection and migration. A limited local configuration read showed development mode, `127.0.0.1:3306/meal_management`, Gmail selected, real sending disabled, and automatic sending left at its disabled default. No passwords or QR keys were printed.
- Back up the target, inspect its migration ledger, review migration 006, and apply only the approved pending changes with a migration-capable account. Stop the old admin sender during the upgrade and coordinate application restarts. No migration has been applied by this phase.
- Keep the existing QR encryption keys, staff, employee, meal, scanner, and email records. Hosted Aiven setup remains separate; its database has not been connected or populated.
- Run the 97 MySQL integration tests only against a separately approved disposable database namespace. Never use the application database for destructive integration tests. Exercise both fresh schema creation and upgrade from a backed-up existing schema.
- Enable `EMAIL_SEND_ENABLED` only after approval. Leave `EMAIL_AUTO_SEND_ENABLED=false` for direct single sends and browser-driven approved bulk processing. Optional automatic recovery can later be enabled for approved bulk/legacy messages only.
- Verify a reviewed single recipient and a reviewed bulk batch with explicit real-email authorization. Check provider failures, lost responses, pending approvals, database commit recovery, and dashboard status. Do not infer inbox delivery from SMTP acceptance.
- Verify the dashboard and scanner on actual laptop and phone browsers, including photo capture and QR scanning. Network exposure and HTTPS changes still need separate approval.
- Complete hosted MySQL, private production storage, HTTPS/proxy trust, backups and restore drills, monitoring, delivery/bounce handling, and deployment verification before claiming production readiness. AWS access and resources remain optional later work.

See [email workflow](email-delivery-workflow.md), [Gmail setup](gmail-setup.md), [Aiven setup](aiven-setup.md), [API contracts](api.md), and [deployment instructions](deployment.md). Skipped integration tests and unavailable manual checks are not counted as passes.
