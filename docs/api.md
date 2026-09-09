# HTTP API

Two independent processes expose separate HTTP applications. `meal_management.admin_api:create_app` serves the administrator dashboard at `APP_ORIGIN`, normally `http://localhost:8000/`. `meal_management.scanner_api:create_app` serves only the scanner at `SCANNER_ORIGIN`, normally `http://localhost:8001/`. Each serves its own assets and calls the same MySQL services directly. No request from the scanner depends on the administrator process.

The scanner has no staff login or service-station selectors. Its `/api/scanner` routes use a signed browser cookie for request ownership and a restricted shared `Meal Scanner` identity for database attribution. The administrator application does not mount these routes or a `/scan` page. The scanner application does not mount authentication, employee, catalog, email, reporting, legacy scan, or administrator pages and routes. Direct master meals require four visitor fields and no per-serving administrator approval. Older authenticated scan and authorization APIs remain available only on the administrator server for compatibility.

## Public scanner

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/scanner/session` | Public same-origin scanner bootstrap | Issue or renew a signed HTTP-only browser cookie; return `scope` and `csrf_token`, with no staff login or database write |
| `POST` | `/api/scanner/read` | Scanner cookie, scanner CSRF, exact origin | Accept only `request_id` and `token`; record one employee meal or request the master visitor form |
| `POST` | `/api/scanner/visitors` | Scanner cookie, scanner CSRF, exact origin | Accept only `request_id`, `token`, and the four required `visitor_details`; record one direct master meal |
| `GET` | `/api/scanner/requests/{request_id}/result` | Original scanner browser | Recover only that browser's original committed result without recording another meal |
| `GET` | `/api/scanner/recovery` | Original scanner browser | Find that browser's last saved request ID when local recovery storage is missing; return no QR token or visitor details |

The scanner's transport manages bootstrap automatically. It sends the scanner-specific `csrf_token` as `X-CSRF-Token`; staff CSRF tokens are not interchangeable. Cross-site bootstrap requests are rejected before issuing cookies. Production cookies use the `__Host-meal_scanner` name; development HTTP uses `meal_scanner`. Cookies are HTTP-only, SameSite Strict, path `/`, and have a 30-day browser lifetime renewed at bootstrap. `scope` is a nonsecret hash used to name browser-local recovery markers; it is not an authentication credential and cannot replace the cookie.

The backend chooses its shared scanner account, location/counter, and default `Meal` type from `scan_app_settings`. Requests cannot choose or override staff identity, role, scanner, location, meal type, quantity, or administrator authorization. The dedicated scanner account has no password and cannot sign in, obtain staff sessions, or acquire administrator roles. The public routes expose no employee metadata, QR export, private photo, catalog administration, or unrestricted reports.

Master `visitor_details` contains exactly `company_name`, `name`, `email`, and `phone`, validated as described below. Employee reads return the canonical committed ScanResult. Master reads return `kind=MASTER`, `next=VISITOR_DETAILS`, and the same UUID without approving or recording a meal. Each UUID is first bound to a hashed browser identity and captured server settings. Another browser cannot submit or recover that UUID. Normalized visitor fields become immutable when the meal request is processed. Repeated successful calls return the original meal and serving IDs; only an explicit new serving uses a new UUID.

The browser retains only scope, request UUID, and quantity one across reloads. A stable pending marker detects changed or lost cookies even after reload and blocks a new meal until the earlier result is checked. QR values and visitor contact details are not stored in browser storage. In-memory and restored retries treat malformed rescans as unconfirmed, including when the original request is still delayed. When local storage has no marker, `/api/scanner/recovery` checks MySQL for the browser's most recent mapped request before new scans are enabled. This supports moving from the old port 8000 scanner to port 8001 without silently losing a committed request. A recovered result still requires **Scan next meal** before another serving. Removing the browser cookie as well as its recovery storage removes the ownership proof; an administrator must check history in that case.

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
| `POST` | `/api/employees` | Admin | Register employee, issue their QR, and queue encrypted email atomically |
| `GET` | `/api/employees/{employee_id}` | Admin | Retrieve employee metadata |
| `PATCH` | `/api/employees/{employee_id}` | Admin | Update supplied employee fields |
| `DELETE` | `/api/employees/{employee_id}` | Admin | Deactivate the employee without deleting records |
| `POST` | `/api/employees/{employee_id}/activate` | Admin | Reactivate the employee |
| `GET` | `/api/employees/{employee_id}/qr` | Admin | Retrieve current QR metadata and renderable SVG when usable |
| `POST` | `/api/employees/{employee_id}/qr` | Admin | Issue a QR only when no unrevoked employee QR exists |
| `POST` | `/api/employees/{employee_id}/qr/replace` | Admin | Revoke the old QR, issue another, and queue its email atomically |
| `POST` | `/api/employees/{employee_id}/qr/revoke` | Admin | Revoke the current QR using the submitted `reason` |
| `POST` | `/api/employees/{employee_id}/qr/resend` | Admin | Queue the same active QR again; return 202 |
| `POST` | `/api/employees/{employee_id}/photo` | Admin | Upload a bounded JPEG/PNG body and update its private storage reference |
| `GET` | `/api/employees/{employee_id}/photo` | Admin | Read the employee's private photo |

Registration requires `employee_code`, `full_name`, `email`, and `department_id`. It accepts optional `selfie_object_key` and timezone-aware `expires_at`. Registration returns `employee_id`, `qr_id`, and `email_id`. Employee updates accept the same employee fields except expiry; only `selfie_object_key` may be explicitly cleared with null.

QR issuance and replacement accept an expiry object with optional `expires_at`. An empty object means no expiry. QR responses include `credential_status`; unusable credentials return metadata with `svg` set to null instead of exposing a revoked or expired token. Revoked employee credentials are retained in history but are no longer the current unrevoked employee QR.

Photo upload expects raw binary data with `Content-Type: image/jpeg` or `image/png`, not JSON or multipart form data. Photos are decoded, validated, re-encoded, and saved through the configured private storage adapter. No employee self-service QR retrieval endpoint exists; waiter sessions cannot retrieve employee QRs or administer employees.

## Master QR and legacy visitor approvals

The scan-only application uses direct visitor entry. It does not create or require the authorizations below. These endpoints preserve the older scoped authorization workflow and its historical records.

| Method | Path | Access | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/master-qrs` | Admin | Paginated master credential metadata |
| `POST` | `/api/master-qrs` | Admin | Issue a master credential with optional expiry |
| `GET` | `/api/master-qrs/{qr_id}` | Admin | Retrieve master metadata and SVG when usable |
| `POST` | `/api/master-qrs/{qr_id}/replace` | Admin | Revoke and replace the specified master credential |
| `POST` | `/api/master-qrs/{qr_id}/revoke` | Admin | Revoke the master using the submitted reason |
| `POST` | `/api/visitor-authorizations` | Admin | Approve one visitor serving for a named waiter |
| `GET` | `/api/visitor-authorizations` | Admin or waiter | Paginated approvals; waiters see only their own |
| `POST` | `/api/visitor-authorizations/{authorization_id}/revoke` | Admin | Revoke an approval |

Creating an approval requires `request_id`, `master_qr_id`, `waiter_id`, `scanner_code`, `meal_type_id`, `quantity`, and `visitor_name`. Optional fields are `visitor_organization`, `visit_purpose`, and `expires_at`. The response contains `authorization_id` and the request UUID. The authorizing administrator is taken from authentication, never from request data. The server obtains the master token internally after checking admin access.

Approval listing accepts `status=pending` or `status=all`. Pending rows are unrevoked, unexpired approvals whose request remains pending. The waiter submits the assigned approval's UUID, quantity, meal type, scanner, and authorization identifier when scanning the master QR.

## Existing staff-authenticated scan contract

`POST /api/scan-app/read` remains compatible with earlier staff clients; the current public scanner uses `/api/scanner/read` instead. This older endpoint requires an administrator or waiter session and accepts exactly `request_id`, `token`, `meal_type_id`, and `scanner_code`. The server determines the category from the private credential. An employee read records one meal and returns the normal committed scan result. A valid, pending master read returns `kind=MASTER`, `next=VISITOR_DETAILS`, and the same `request_id`. This response is not meal approval. The master read is logged as `AWAITING_DETAILS` and the request remains pending. Invalid or unusable credentials return recorded rejection results. Retrying a completed read returns the original committed result.

`POST /api/scans` requires an administrator or waiter session and accepts `request_id`, `token`, `meal_type_id`, `scanner_code`, optional `quantity` defaulting to one, optional `authorization_id` for the legacy workflow, and optional `visitor_details`. For direct master meals, `visitor_details` requires exactly `company_name`, `name`, `email`, and `phone`. Company and name allow up to 150 characters, email up to 254, and phone up to 32 characters with 7–15 digits and common phone punctuation. Text is trimmed and email lowercased. Direct master meals require quantity one and no authorization ID. Employee scans reject visitor details. Do not include staff IDs or roles. The backend derives the location from the registered active scanner.

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
| `GET` | `/api/email-settings` | Admin | Safe backend, sending and automatic-delivery flags, worker-running status, poll interval, batch size, and preview availability; no sender address or credentials |
| `GET` | `/api/email-queue` | Admin | Paginated safe queue metadata, optionally filtered by `employee_id` |
| `GET` | `/api/email-queue/{email_id}/preview` | Admin in development preview mode | Render an employee email without sending it |

Paginated endpoints accept `limit` from 1 to 1,000 and an `after_id` cursor. Responses contain `items` and `next_cursor`, which is null when there is no further page. Report endpoints require timezone-aware `start` and `end`; the start is inclusive and the end is exclusive. Scan outcomes are `RECEIVED`, `AWAITING_DETAILS`, `SUCCESS`, or `REJECTED`. Duplicate reads appear as `DUPLICATE_REQUEST` scan rejections while the serving response confirms the previously approved meal. Meal history includes `visitor_company_name`, `visitor_name`, `visitor_email`, and `visitor_phone`; legacy visitor names and organizations remain visible through their original authorization joins. Direct visitor meals count in totals without an invented authorizing administrator.

Errors have an `error` object containing a safe code, message, and correlation request identifier. Typical statuses are 401 for missing or invalid authentication, 403 for access or request-protection failures, 422 for malformed request data, 409 for conflicting lifecycle/idempotency operations, and 503 for unavailable or unconfirmed processing. Database exception details and credentials are not included. Valid scan business rejections use the scan-result contract rather than implying an HTTP transport failure.

There is no browser send-now endpoint. Preview access leaves the queue unsent. With `EMAIL_SEND_ENABLED=true` and `EMAIL_AUTO_SEND_ENABLED=true`, the admin process polls committed queued messages in a background thread and dispatches them through Gmail or SES. The scanner never starts this worker. The guarded `send-email` CLI command remains available for one explicitly selected queue ID with separate database-change and real-email approval flags. Both paths use the same durable claims and never automatically retry failed or uncertain attempts. See [Gmail setup and recovery](gmail-setup.md) and the [deployment guide](deployment.md#private-local-photos-and-email-previews).
