# Development fixtures and waiter testing

The application continues to use MySQL 8.4. No fixture insertion, migration, or network-exposure change happens on startup. Default preview configuration sends no real email and performs no AWS operation. Separately enabling real automatic email causes the admin process to poll the queue and contact the configured provider. Confirm the exact target development database before applying pending migrations or inserting the fixtures. The existing administrator remains the administrator; the seed does not bootstrap or replace that account.

## Prepare the database after approval

Keep the runtime account in the private `.env`. Check configuration and review the migration plan from the project directory without opening a database connection:

```sh
.venv/bin/python manage.py --env-file .env check-config
.venv/bin/python manage.py migration-plan
```

Migration `003_development_test_data.sql` adds ownership records and encrypted copies of fixture QR credentials. These copies let an administrator download deliberately revoked or expired test QRs without weakening normal employee QR retrieval. The migration does not insert employees, staff accounts, or test QRs. Published migrations 001 and 002 remain unchanged.

After the target database and schema change are approved, apply pending migrations interactively:

```sh
.venv/bin/python manage.py --env-file .env migrate --database-user root --allow-database-changes
```

Migration `004_direct_master_visitor_meals.sql` adds the four visitor contact fields, request binding, and pending master-read logging for the separate scan application. It preserves old employee and authorized visitor meals. A failed migration must be inspected before retrying; follow the recovery guidance in `deployment.md`.

Migration `005_shared_meal_scanner.sql` is also required for scanning without login or service selection. It creates the dedicated `Meal Scanner` system identity, its default scanner/location and `Meal` type, and immutable browser request mappings. For a database at version 002, pending versions are 3, 4, and 5; at version 004, only 5 is new. These are application defaults, not test employees or email recipients. Existing human accounts, catalog records, employee QRs, and meal history are preserved. The migration refuses reserved-code collisions rather than overwriting records.

## Insert the development fixtures after target confirmation

The command requires `APP_ENV=development`, `EMAIL_BACKEND=preview`, and `EMAIL_SEND_ENABLED=false`. It uses the configured application database account. It refuses production and live-email configurations before connecting.

```sh
.venv/bin/python manage.py --env-file .env seed-development --allow-database-changes
```

Read the displayed host, port, and database name. Enter the exact target string only after confirming that this is the intended development database. Enter the existing application administrator email and password when prompted. On the first run, choose and confirm a separate password for each test waiter. Password entry is hidden and no passwords are printed or stored in plaintext. Do not paste passwords or private environment values into chat.

The seed creates its records in one transaction through the existing staff, employee, catalog, and QR services. It refuses collisions with unowned records. A successful rerun preserves the original fixture IDs, QR tokens, account passwords, and email queue entries. It does not restore fixtures that an administrator deliberately changes later. Conflicting or inconsistent ownership requires investigation rather than overwriting records.

The expired fixture is initially issued with a short valid lifetime through the normal QR service. The command waits briefly after commit so that the expiry case is ready for testing. No system clock, existing QR expiry, or immutable record is changed.

| Fixture | Expected behavior |
| --- | --- |
| `TEST-EMP-001` through `TEST-EMP-007` | Active, reusable employee QRs; each explicit new serving records one meal |
| `TEST-EMP-008` | Inactive employee; scan rejects with `EMPLOYEE_INACTIVE` |
| `TEST-EMP-009` | Revoked QR; scan rejects with `QR_REVOKED` |
| `TEST-EMP-010` | Expired QR; scan rejects with `QR_EXPIRED` |
| `test.waiter.one@example.test` | First test waiter with only the existing `WAITER` role |
| `test.waiter.two@example.test` | Second test waiter with only the existing `WAITER` role |
| `TEST Admin Office` | Reusable master QR; requests Company Name, Name, Email, and Phone, then records one meal without per-serving admin approval |
| `TEST Cafeteria`, `TEST Lunch`, `TEST Scanner A`, `TEST Scanner B` | Registered service settings for the scanning exercises |

All employee email addresses use `example.test`. Registration queues local previews through the normal encrypted email queue. Inactive and revoked fixtures cancel their queued QR messages through the normal services; expired credentials cannot be previewed as valid emails. Use the development QR gallery to obtain these intentionally invalid test images. No sender or email delivery worker is invoked.

## Laptop checks

After approved database preparation and the frontend build, finish or reconcile any pending serving and stop the old combined development server with Control+C in its own terminal. Keep its scanner cookie and existing browser storage. Start the administrator from the project folder in Terminal 1:

```sh
sh deploy/start-admin.sh
```

Open the exact configured `APP_ORIGIN`, normally `http://localhost:8000`, on the laptop. Sign in as the administrator and open **Development QRs**. View or download test QR images, including the intentionally invalid cases. The gallery and its image endpoints require an administrator and are disabled outside development preview mode. Keep downloaded active QRs private.

Start the scanner from the same project directory in Terminal 2:

```sh
sh deploy/start-scanner.sh
```

Open `http://localhost:8001`. The camera controls appear without login, account controls, location, station, or meal selection. The scanner connects directly to MySQL using the same private configuration as the administrator process and records under its dedicated `Meal Scanner` system identity and default `Meal` type. The dashboard at `http://localhost:8000/#dashboard`, QR exports, and unrestricted reports still require administrator login. The test waiter accounts remain available for testing the older staff-authenticated API and its restrictions on the admin server.

Browser storage is separate for each port. If the port 8001 scanner has no local marker, it uses its preserved signed browser cookie to find the latest stored request and recover its result before another meal can begin. This can show an already completed previous serving; select **Scan next meal** only when deliberately starting another serving. A failed recovery lookup blocks scanning. Neither launcher kills an older server or any unrelated process.

1. Select **Start camera** and grant permission. Show an active fixture QR from another display or a printout. Confirm the large processing state appears before **Accepted · 1 meal**. Verify the employee identity and recorded time in the administrator's meal history.
2. Leave the same QR in front of the camera. Confirm the camera pauses and no second meal appears. Select **Scan next meal** before deliberately scanning the reusable QR again. As admin, verify that this produces two distinct meals.
3. Repeat with the inactive, revoked, and expired fixture images. Confirm rejection reasons and rejected-scan history. Rejected scans must not add meal records or expose an unrelated employee photo.
4. Use the QR-image upload option with a downloaded test image. It must use the same backend scan validation and result states. Test an image without a QR and an unsupported file type.
5. Test **Stop camera** and **Switch camera** where a second camera exists. Deny camera permission, retry after granting permission, and verify the upload fallback remains usable. Navigate away or close the scanner page; the browser camera-use indicator must stop.
6. Scan the test master QR without signing in. Confirm the form has exactly Company Name, Name, Email, and Phone. Enter fictional data using `example.test`, submit once, and wait for committed approval. No administrator approval or quantity entry should be requested. Verify all four fields, quantity one, `Meal Scanner` attribution, default `Meal` type, and time in admin reports. Select **Scan next meal** to reuse the master for another visitor.
7. Attempt missing or invalid visitor fields; no meal should be recorded. Read a master, revoke it as admin before submitting the form, and verify rejection. Confirm a different browser cannot recover the first browser's result or change its pending request. The public scanner must be unable to open employee management, the test QR gallery, or unrestricted report APIs.
8. Interrupt the browser connection around a scan submission. No approval may appear without a confirmed backend result. Restore the connection and use the same serving retry. Verify that the original meal IDs are returned and only one serving exists. Do not use a new serving identifier to resolve an uncertain result.
9. During an uncertain serving, reload and upload an unrelated QR containing a web address. The scanner must keep the previous serving unresolved, preserve its request identifier, and refuse to start a new meal. Rescan the original employee or master QR and recover its original result before continuing.
10. During another uncertain serving, remove only the scanner cookie while preserving session storage, then reload. The scanner must detect the changed browser scope and block new meals. Have the administrator inspect the recorded history before resolving the stale marker. Do not clear all storage or start another serving just to hide an uncertain result.
11. Stop only the administrator with Control+C in Terminal 1. Leave MySQL and the scanner running. Record an employee serving and a master visitor serving from Terminal 2's application. Restart the administrator, sign in, refresh history, and verify both records, all visitor fields, and recorded timestamps. This is the real database independence check; process liveness alone does not establish it.
12. On port 8001, request `/api/employees`, `/api/reports/meals`, `/api/auth/me`, and `/assets/app.js`; each must return 404 even if this browser is signed into the admin application. Port 8000 must return 404 for `/api/scanner/session`. After logout, admin report APIs must require authentication.

Reload the page once during the interrupted-connection exercise in the same browser. The pending marker contains only its browser scope, request identifier, and quantity one. The scanner recovers a finalized result or asks for the original QR again while retaining that identifier. It must not approve offline or enable a new meal while the previous result remains unresolved. A mismatched QR cannot replace the original serving. Session storage can be removed when a browser session closes or storage is cleared; investigate history if an uncertain marker is lost.

The public scanner exposes only its own result recovery endpoint, with no employee photo or contact lookup. Older staff photo and receipt APIs still require the original authenticated caller's committed serving. Possession of an employee ID or another browser's request UUID does not grant access.

## Employee registration and selfie checks

From the administrator application, open **Employees → Add employee** and verify the eight facility names in the department list. Existing active departments should still be selectable. Opening the form must not create department or employee records. With approved development data, select a facility department and register a fictional employee; confirm the saved employee references the actual department record. Repeat with another employee in the same department and verify no duplicate department is created. Keep deliberately inactive departments inactive.

In the same registration dialog, choose **Take photo** and grant camera access. Confirm the preview uses the front-facing camera where available. Capture the image, review it, and choose **Use photo**; register the fictional employee and verify the private photo in employee details. Test **Retake**, **Cancel**, an existing uploaded photo followed by a cancelled capture, and a denied or unavailable camera with file upload as the fallback. Use only test photos appropriate for the development database.

Close the dialog while the camera is active and while its permission request is pending. Switch browser tabs, navigate to another dashboard page, and sign out. The camera must stop, including when a delayed permission response arrives after the dialog closed. Also open an employee's Manage dialog on a slow connection, close it, and open registration; a delayed employee-details response must not replace the registration form or leave a camera running.

Automated media-device tests use simulated cameras and image blobs. Actual camera permission prompts, orientation, preview appearance, private upload, and retrieval require these manual checks. This code change does not insert fixture departments or employees, capture a real photo, or connect to MySQL during automated verification.

## Phone checks

A phone's `localhost` refers to the phone, not the laptop. The default loopback-only server cannot be reached from another device. Camera access generally needs a secure context; the localhost exception applies to the device loading the page. See the [browser camera security requirements](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/getUserMedia#privacy_and_security).

Before phone testing, obtain separate approval for network exposure and HTTPS configuration. If extra certificate tooling or a tunnel is chosen, installation and tunnel creation also need approval. Nothing in this change binds the server to the LAN, installs certificates, opens firewall ports, or creates a tunnel.

After an approved phone-access setup is in place:

1. Use the approved laptop hostname and separate scanner HTTPS URL from the phone on the intended network. Configure a certificate trusted by that phone, exact `SCANNER_ORIGIN`, `APP_ORIGIN`, and `ALLOWED_HOSTS`, and secure cookies. Do not disable browser security or certificate verification to make the camera work. The provided local launchers remain loopback HTTP only; a phone setup requires its own approved configuration.
2. Open the scanner application's root URL directly without signing in. Verify the visitor form and result state fit portrait and landscape screens and remain usable with the on-screen keyboard. No station or meal selector should appear.
3. Start the camera and verify the rear camera is preferred. Test switching to another available camera and back, stopping, restarting, permission denial, and no-camera handling. Device availability depends on browser permission; see [camera enumeration behavior](https://developer.mozilla.org/en-US/docs/Web/API/MediaDevices/enumerateDevices).
4. Scan fixture QRs displayed on the laptop or printed on paper. For image upload, save a fixture image on the phone first. The phone still needs a connection to the backend; upload is not offline approval.
5. Repeat employee reuse, invalid QR, master visitor form, and interrupted-connection exercises from the laptop checklist. Verify the browser camera indicator stops after leaving the page, closing the tab, or backgrounding where supported.

## Automated verification

Run the offline Python tests and frontend tests/build separately:

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests/unit -v
npm --prefix frontend test
npm --prefix frontend run build
```

MySQL integration tests live separately under `tests/integration`. They require an explicitly approved disposable test database namespace and a dedicated account with appropriate privileges. Follow the existing integration guards in the project README. Do not enable those flags against the application database merely to test this feature.

The separate process tests under `tests/local` start temporary localhost listeners using fictional configuration and block all database connections. Run `PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests/local -v` to check independent startup and occupied-port handling. The tests terminate only their own child processes.

Passing isolated API/service tests and camera lifecycle tests does not verify actual camera hardware, phone HTTPS trust, MySQL locking, migrations, or real fixture insertion. Record those results separately after the relevant approval and manual exercises.
