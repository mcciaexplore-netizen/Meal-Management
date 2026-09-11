# HTTP API

Two independent processes expose separate HTTP applications. `meal_management.admin_api:create_app` serves the administrator dashboard at `APP_ORIGIN`, normally `http://localhost:8000/`. `meal_management.scanner_api:create_app` serves only the scanner at `SCANNER_ORIGIN`, normally `http://localhost:8001/`. Each serves its own assets and calls the same MySQL services directly. No request from the scanner depends on the administrator process.

The scanner has no staff login or service-station selectors. Its `/api/scanner` routes use a signed browser cookie for request ownership and a restricted shared `Meal Scanner` identity for database attribution. The administrator application does not mount these routes or a `/scan` page. The scanner application does not mount authentication, employee, catalog, email, reporting, legacy scan, or administrator pages and routes. An administrator creates each master QR with visitor-group contact details and a meal allowance; the scanner collects no visitor details and requires no per-serving approval. Older authenticated scan and authorization APIs remain available only on the administrator server for compatibility.

## Public scanner

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/scanner/session` | Public same-origin scanner bootstrap | Issue or renew a signed HTTP-only browser cookie; return `scope` and `csrf_token`, with no staff login or database write |
| `POST` | `/api/scanner/read` | Scanner cookie, scanner CSRF, exact origin | Accept only `request_id` and `token`; record one employee meal or consume one remaining master meal |
| `GET` | `/api/scanner/requests/{request_id}/result` | Original scanner browser | Recover only that browser's original committed result without recording another meal |
| `GET` | `/api/scanner/recovery` | Original scanner browser | Find that browser's last saved request ID when local recovery storage is missing; return no QR token or visitor details |

The scanner's transport manages bootstrap automatically. It sends the scanner-specific `csrf_token` as `X-CSRF-Token`; staff CSRF tokens are not interchangeable. Cross-site bootstrap requests are rejected before issuing cookies. Production cookies use the `__Host-meal_scanner` name; development HTTP uses `meal_scanner`. Cookies are HTTP-only, SameSite Strict, path `/`, and have a 30-day browser lifetime renewed at bootstrap. `scope` is a nonsecret hash used to name browser-local recovery markers; it is not an authentication credential and cannot replace the cookie.

The backend chooses its shared scanner account, location/counter, and default `Meal` type from `scan_app_settings`. Requests cannot choose or override staff identity, role, scanner, location, meal type, quantity, or administrator authorization. The dedicated scanner account has no password and cannot sign in, obtain staff sessions, or acquire administrator roles. The public routes expose no employee metadata, QR export, private photo, catalog administration, or unrestricted reports.

Employee and master reads return the canonical committed ScanResult. A master allowance of five permits five successful servings with distinct request UUIDs, one meal each. Each serving snapshots the group's stored company, contact name, email, and phone into meal history. The fifth committed meal exhausts that QR; later new requests return `QR_EXPIRED`, displayed as “QR has expired. Please contact the administrator.” Allowance consumption, the serving, its meal, and the completed scan result commit together. Concurrent scans lock the same allowance and cannot consume its final meal twice. Existing master QRs without group allocations are retained for history and are rejected by the public scanner; create a new group QR in the admin application.

Each UUID is first bound to a hashed browser identity and captured server settings. Another browser cannot submit or recover that UUID. Repeated successful calls return the original meal and serving IDs without consuming another allowance, including after the QR is exhausted. Only a deliberate new serving uses a new UUID. `/api/scanner/visitors` is no longer exposed; public callers cannot replace the administrator's stored visitor details.

The browser retains only scope, request UUID, and quantity one across reloads. A stable pending marker detects changed or lost cookies even after reload and blocks a new meal until the earlier result is checked. QR values and visitor contact details are not stored in browser storage. In-memory and restored retries treat malformed rescans as unconfirmed, including when the original request is still delayed. When local storage has no marker, `/api/scanner/recovery` checks MySQL for the browser's most recent mapped request before new scans are enabled. This supports moving from the old port 8000 scanner to port 8001 without silently losing a committed request. A confirmed approval or rejection displays an animation for two seconds, then returns to the initial scanner screen with the camera stopped. Starting the camera or uploading the next QR deliberately begins another serving; an unchanged QR left in view cannot generate repeated meals. If the completed recovery marker cannot be cleared, the result remains visible with **Scanner reset paused** and a **Return to scanner** retry. Processing or connection errors keep the original request unresolved and never reset it as a new meal. Removing the browser cookie as well as its recovery storage removes the ownership proof; an administrator must check history in that case.

`SCAN_APP_ENABLED` defaults to enabled in development and disabled in production. `SCANNER_DISABLED` returns 404, `SCANNER_NOT_CONFIGURED` returns a safe setup message with 503, and `SCANNER_BROWSER_REQUIRED` returns 401 for transparent browser-session recovery. Per-browser and per-address limits use shared MySQL buckets; `SCANNER_RATE_LIMITED` returns 429 with its own `Retry-After`. A validation failure is recorded through the shared scanner identity. Failure to record or commit never returns meal approval.

## Authentication and request protection

The routes in this section and the administrator sections below exist only on port 8000. `GET /api/application` returns the configured `scanner_url` for dashboard links; it contains no credentials and does not proxy scanner requests. Health checks exist separately on both servers. Browser cookies are not scoped by port: the servers therefore use different cookie names and proof formats, ignore the other application's credential, and enforce their own exact origin on mutations. Cross-origin API access is not enabled.

Begin with `GET /api/auth/csrf`, which sets a protected CSRF seed cookie and returns `csrf_token`. Include that value in `X-CSRF-Token` on every state-changing API request, including login and logout. Also send the exact configured `Origin`. The browser frontend manages this sequence automatically.

Successful login sets the HTTP-only session cookie, rotates the CSRF seed, and returns the current staff identity, roles, and new CSRF token. Sessions enforce absolute and idle expiry against MySQL. Login limits are shared across application workers through MySQL and apply to account and client-address buckets. Logout revokes the presented session and clears cookies, including when the session has already expired or been revoked.

All JSON input models reject extra fields. Identifiers and quantities must be positive integers. Datetime inputs must include a timezone offset. API and health responses receive `Cache-Control: no-store` and security headers. QR images, session cookies, passwords, and email previews must not be captured in request or response logs.

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/auth/csrf` | Public | Obtain the CSRF token for the current browser session |
| `POST` | `/api/auth/login` | Public with CSRF/origin protection | Authenticate `email` and `password`; set session cookie |
| `GET` | `/api/auth/me` | Authenticated staff | Return `staff_id`, `display_name`, `email`, and `roles` |
| `POST` | `/api/auth/logout` | CSRF/origin protection | Revoke the current session and clear cookies; return 204 |
| `GET` | `/health/live` | Public | Process liveness without database access |
| `GET` | `/health/ready` | Public | Check MySQL 8.4 and required applied migration versions |

There is no public staff registration or first-admin endpoint. First-admin setup uses `.venv/bin/python manage.py --env-file .env bootstrap-admin --allow-database-access` from the project root after approved database preparation. `manage.py` supplies the backend import path explicitly. See the [README](../README.md#local-setup) for the complete interactive setup sequence.

## Configuration and staff

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/catalog` | Admin or waiter | Return meal types, locations, and scanners; admins also receive departments and active waiter choices |
| `POST` | `/api/catalog/departments` | Admin | Create a department from `name` |
| `POST` | `/api/catalog/locations` | Admin | Create a serving location from `code` and `name` |
| `POST` | `/api/catalog/meal-types` | Admin | Create a meal type from `code` and `name` |
| `POST` | `/api/catalog/scanners` | Admin | Create a scanner from `code`, `name`, and `location_id` |
| `GET` | `/api/staff` | Admin | Paginated staff metadata and assigned roles, without password hashes |
| `POST` | `/api/staff` | Admin | Create a staff account from `display_name`, `email`, `password`, and `roles` |
| `PATCH` | `/api/staff/{staff_id}/active` | Admin | Set `is_active`; deactivation revokes sessions |

The last active administrator cannot be disabled through the staff service. Either `ADMIN` or `WAITER` permits scanning. Helpers use `WAITER`; there is no helper role or helper QR category. Active administrators are also eligible as the serving staff member of a visitor authorization. Waiters still cannot administer employees, export QRs, or read unrestricted reports.

## Employees and private QRs

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/employees` | Admin | Paginated employee list, optional `q` and `active` filters |
| `POST` | `/api/employees` | Admin | Register employee, issue their QR, and prepare an encrypted SINGLE DRAFT atomically; no sending |
| `GET` | `/api/employees/{employee_id}` | Admin | Retrieve employee metadata |
| `PATCH` | `/api/employees/{employee_id}` | Admin | Update supplied employee fields |
| `DELETE` | `/api/employees/{employee_id}` | Admin | Deactivate the employee without deleting records |
| `POST` | `/api/employees/{employee_id}/activate` | Admin | Reactivate the employee |
| `GET` | `/api/employees/{employee_id}/qr` | Admin | Retrieve current QR metadata and renderable SVG when usable |
| `POST` | `/api/employees/{employee_id}/qr` | Admin | Issue a QR only when no unrevoked employee QR exists |
| `POST` | `/api/employees/{employee_id}/qr/replace` | Admin | Revoke the old QR, issue another, and prepare its single-email draft atomically |
| `POST` | `/api/employees/{employee_id}/qr/revoke` | Admin | Revoke the current QR using the submitted `reason` |
| `POST` | `/api/employees/{employee_id}/qr/resend` | Admin | Prepare/reuse a single-email draft for the same active QR; return 202 with safe delivery metadata |
| `POST` | `/api/employees/{employee_id}/photo` | Admin | Upload a bounded JPEG/PNG body and update its private storage reference |
| `GET` | `/api/employees/{employee_id}/photo` | Admin | Read the employee's private photo |

Registration requires `employee_code`, `full_name`, `company_name`, `email`, `phone`, and `department_id`. It accepts optional `selfie_object_key` and timezone-aware `expires_at`. Registration returns `employee_id`, `qr_id`, and `email_id`. Employee updates accept the same employee fields except expiry; only `selfie_object_key` may be explicitly cleared with null. Existing employee rows may have empty company or phone data until an administrator supplies it.

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `POST` | `/api/employees/bulk` | Admin | Atomically import 1–100 employees with personal QRs and pending approval emails |
| `POST` | `/api/employees/{employee_id}/emails/{email_id}/send` | Admin | Approve only this employee's SINGLE draft, send synchronously, and return a committed delivery result |
| `GET` | `/api/employees/{employee_id}/emails/{email_id}` | Admin | Recover safe status for the exact employee/email pair without sending |

Bulk import accepts `request_id` and `employees`. Each employee has exactly `employee_code`, `full_name`, `company_name`, `email`, `phone`, and an existing active `department_id`. The response contains `batch_id`, `employees` with employee/QR/email IDs, and `replayed`. The request UUID is bound to the authenticated administrator and normalized ordered payload. An unchanged retry returns the original IDs, while a changed payload is rejected. Any row failure rolls back the entire new batch.

Single sends reuse their durable email ID on every retry and never create another email entry themselves. Results contain `email_id`, `status`, and a safe `code`; statuses include `SENT`, `ALREADY_SENT`, `FAILED`, `CANCELLED`, and `NEEDS_REVIEW`. A provider or connection error is not successful delivery. Follow unknown results through the status endpoint before considering another request. Browser callers cannot choose an approving staff ID or bypass the real-email configuration gate.

Employee QR issuance and replacement accept an expiry object with optional `expires_at`. An empty object means no expiry. QR responses include `credential_status`; unusable credentials return metadata with `svg` set to null instead of exposing a revoked or expired token. Revoked employee credentials are retained in history but are no longer the current unrevoked employee QR.

Photo upload expects raw binary data with `Content-Type: image/jpeg` or `image/png`, not JSON or multipart form data. Photos are decoded, validated, re-encoded, and saved through the configured private storage adapter. No employee self-service QR retrieval endpoint exists; waiter sessions cannot retrieve employee QRs or administer employees.

## Master QR and legacy visitor approvals

The administrator supplies `company_name`, `contact_name`, `email`, `phone`, and `meal_limit` when creating or replacing a master QR. `meal_limit` is a positive whole number up to 65,535; it represents people, with one meal per successful scan. Optional `expires_at` must include a timezone and be in the future. Create a separate QR for each group request. Master metadata includes the group details, limit, used count, remaining meals, and exhaustion time. The scanner does not create or require the legacy authorizations below; they preserve the older scoped authorization workflow and its historical records.

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/master-qrs` | Admin | Paginated master credential metadata |
| `POST` | `/api/master-qrs` | Admin | Issue a master credential with group contact details, meal allowance, and optional expiry |
| `GET` | `/api/master-qrs/{qr_id}` | Admin | Retrieve master metadata and SVG when usable |
| `POST` | `/api/master-qrs/{qr_id}/replace` | Admin | Revoke the specified credential and create a replacement with newly supplied group details and allowance |
| `POST` | `/api/master-qrs/{qr_id}/revoke` | Admin | Revoke the master using the submitted reason |
| `POST` | `/api/visitor-authorizations` | Admin | Approve one visitor serving for a named waiter |
| `GET` | `/api/visitor-authorizations` | Admin or waiter | Paginated approvals; waiters see only their own |
| `POST` | `/api/visitor-authorizations/{authorization_id}/revoke` | Admin | Revoke an approval |

Creating an approval requires `request_id`, `master_qr_id`, `waiter_id`, `scanner_code`, `meal_type_id`, `quantity`, and `visitor_name`. Optional fields are `visitor_organization`, `visit_purpose`, and `expires_at`. The response contains `authorization_id` and the request UUID. The authorizing administrator is taken from authentication, never from request data. The server obtains the master token internally after checking admin access.

Approval listing accepts `status=pending` or `status=all`. Pending rows are unrevoked, unexpired approvals whose request remains pending. The waiter submits the assigned approval's UUID, quantity, meal type, scanner, and authorization identifier when scanning the master QR.

## Existing staff-authenticated scan contract

`POST /api/scan-app/read` remains compatible with earlier staff clients; the current public scanner uses `/api/scanner/read` instead. This older endpoint requires an administrator or waiter session and accepts exactly `request_id`, `token`, `meal_type_id`, and `scanner_code`. The server determines the category from the private credential. Employee reads and allocated master reads record one meal and return the committed result. An older master credential without a group allocation retains its legacy `kind=MASTER`, `next=VISITOR_DETAILS`, and `AWAITING_DETAILS` behavior on this staff-only endpoint. Invalid or unusable credentials return recorded rejection results. Retrying a completed read returns the original committed result.

`POST /api/scans` requires an administrator or waiter session and accepts `request_id`, `token`, `meal_type_id`, `scanner_code`, optional `quantity` defaulting to one, optional `authorization_id` for the legacy workflow, and optional `visitor_details`. Allocated master QRs require quantity one and reject supplied visitor details or authorization IDs; this endpoint cannot bypass their shared meal allowance. For older unallocated master QRs, direct `visitor_details` requires exactly `company_name`, `name`, `email`, and `phone`. Company and name allow up to 150 characters, email up to 254, and phone up to 32 characters with 7–15 digits and common phone punctuation. Text is trimmed and email lowercased. Legacy direct master meals require quantity one and no authorization ID. Employee scans reject visitor details. Do not include staff IDs or roles. The backend derives the location from the registered active scanner.

A processed scan returns `approved`, `code`, `request_id`, `serving_id`, `meal_ids`, and `duplicate`. Employee and direct master scans create one meal. Legacy authorized master scans retain one meal per approved unit. Business rejections return `approved=false` with their recorded reason and no new meal. Invalid authenticated scan input is logged where the database is available.

Reuse the exact request UUID from the initial QR read for the visitor form, and retain it for retries. A successful retry returns the original approval, serving ID, and meal IDs with `duplicate=true`; it is not permission to hand out an additional meal. The server binds retries to the authenticated caller, original serving details, and the normalized visitor fields from the first final submission. Completed retries use original receipt metadata to verify scanner location even if that scanner has subsequently moved. Changing the submitted scanner code or other bound inputs produces an idempotency rejection.

`PROCESSING_UNCONFIRMED` and `SCAN_RECEIPT_UNCONFIRMED` are HTTP 503 errors from scan submission. Keep the original request and retry rather than generating another UUID. The browser retains a caller-bound pending marker in session storage with the request ID and nonsecret serving fields. QR credentials and visitor contact details are kept only in memory. After reload or a return from logout, the browser recovers the original result or requires the same QR to be read again using the same identifier and frozen serving details. An unresolved visitor form may require the original details to be re-entered. Clearing browser storage or closing its session can remove the marker; verify history before starting a new serving if the previous result was uncertain.

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/scans/{request_id}/result` | Original serving caller, admin or waiter | Recover the original committed ScanResult; a pending result stays unapproved with `PROCESSING_UNCONFIRMED` |
| `GET` | `/api/scans/{request_id}/receipt` | Original serving caller, admin or waiter | Read a completed serving's employee or visitor name, quantity, and UTC time |
| `GET` | `/api/scans/{request_id}/photo` | Original serving caller, admin or waiter | Read an available employee photo only after validating the committed serving |

Recovery endpoints do not record additional meals. Another staff member cannot use the request ID to read the receipt or photo, even if that staff member is also an administrator. Rejected or pending requests do not expose employee details. A malformed rescan against an existing UUID logs its actual validation error but returns nonterminal `REQUEST_MISMATCH`, preserving the original result. A photo or receipt loading failure does not undo a confirmed meal approval.

## Development QR gallery

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/development/test-data` | Admin in development preview mode | Return metadata for seed-owned employees, waiters, and the master QR |
| `GET` | `/api/development/test-data/qrs/{qr_id}` | Admin in development preview mode | Render an archived seed-owned QR image, including intentionally invalid fixtures |

The gallery is disabled unless the environment is development, the email backend is preview, and real sending is disabled. `GET /api/catalog` exposes `development_test_data=true` only to administrators in that configuration. The routes cannot render unrelated employee or master credentials. There is no seed mutation API. Fixture creation requires the interactive development command, target confirmation, and existing administrator authentication described in [development testing](development-testing.md).

## Reports and email queue

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/reports/meals` | Admin | Paginated individual meal history, optionally filtered by `employee_id` |
| `GET` | `/api/reports/totals` | Admin | Meal and serving totals, grouped by kind, meal type, location, waiter, and authorizing admin |
| `GET` | `/api/reports/scans` | Admin | Paginated scan attempts, optionally filtered by `outcome` |
| `GET` | `/api/email-settings` | Admin | Safe backend, sending/automatic flags, approval_required=true, worker status, poll interval, batch size, and preview availability; no credentials |
| `GET` | `/api/email-queue` | Admin | Paginated bulk/legacy metadata by default; employee_id returns full employee history; bulk_batch_id filters one import |
| `GET` | `/api/email-queue/{email_id}/preview` | Admin in development preview mode | Render an employee email without sending it |

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/email-queue/{email_id}` | Admin | Safe delivery status without encrypted payloads or internal claim markers |
| `POST` | `/api/email-queue/approve` | Admin | Approve 1–100 explicit bulk/legacy email IDs atomically; records the authenticated administrator and UTC time |
| `POST` | `/api/email-queue/process` | Admin | Process 1–10 explicit already-approved bulk/legacy IDs after validating the whole selection |

Approval and processing accept only `email_ids`, a nonempty array of distinct positive integer IDs. Approval returns `email_ids` and `approved_count`; processing returns `results`, each with the single-worker result shape. Approval alone does not call the provider. The dashboard follows approval with bounded process calls; an enabled optional dispatcher may process already-approved rows concurrently using the same claims. No action selects unapproved entries or new employees implicitly. Failed or uncertain deliveries are never automatically retransmitted. Single drafts are excluded from the global queue and cannot be approved through bulk endpoints. Migration 006 is required. See [email delivery workflow](email-delivery-workflow.md).

Paginated endpoints accept `limit` from 1 to 1,000 and an `after_id` cursor. Responses contain `items` and `next_cursor`, which is null when there is no further page. Report endpoints require timezone-aware `start` and `end`; the start is inclusive and the end is exclusive. Scan outcomes are `RECEIVED`, `AWAITING_DETAILS`, `SUCCESS`, or `REJECTED`. Duplicate reads appear as `DUPLICATE_REQUEST` scan rejections while the serving response confirms the previously approved meal. Meal history includes `employee_company_name`, `employee_email`, and `employee_phone` from the employee profile, plus `visitor_company_name`, `visitor_name`, `visitor_email`, and `visitor_phone` from each visitor serving's snapshot. Legacy visitor names and organizations remain visible through their original authorization joins. Direct visitor meals count in totals without an invented authorizing administrator.

Errors have an `error` object containing a safe code, message, and correlation request identifier. Typical statuses are 401 for missing or invalid authentication, 403 for access or request-protection failures, 422 for malformed request data, 409 for conflicting lifecycle/idempotency operations, and 503 for unavailable or unconfirmed processing. Database exception details and credentials are not included. Valid scan business rejections use the scan-result contract rather than implying an HTTP transport failure.

Preview access leaves drafts and pending approvals unsent. The administrator's single Send email action approves and processes only its bound email ID. Bulk approval commits before processing starts. With both sending flags enabled, the optional admin thread processes only already-approved BULK or LEGACY entries; it never selects single drafts. The scanner exposes no email actions and never starts a sender. The guarded `send-email` CLI command also requires a recorded approval and cannot bypass it. Every delivery path uses the same durable claims and never automatically retries failed or uncertain attempts. See [Gmail setup and recovery](gmail-setup.md) and the [deployment guide](deployment.md#private-local-photos-and-email-previews).
