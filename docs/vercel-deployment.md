# Deploy the two applications to Vercel

The user has connected both existing projects to the same Git repository. Follow [the Git deployment instructions](vercel-git-deployment.md) for the current deployment path. Both project roots still require configuration and the new files must be committed and pushed before a Git deployment can use them. The earlier CLI build attempts failed; do not repeat them as the next step.

The selected architecture is two independent Vercel projects built from the same source project: one administrator application and one scanner application. Both use the already restored Aiven MySQL 8.4 database. No third application server is required. Vercel Blob provides private photo storage; Gmail remains the email provider. Personal AWS credentials are not required.

This is deployment preparation. On 2026-09-10 the user created the `mccia-meal-photos` Blob store in the `MCCIA's projects` team. Its dashboard was verified as Private, region BOM1, with 0 B stored. The store ID is `store_psdHbdPb2VXd56FQ`; this dashboard ID is not the private Blob hostname used by the application. Both meal application projects are connected to the store for Production with the required read/write token variable. Neither application is deployed, and no employee photos have been uploaded. The existing private environment files remain unchanged. Do not import the database again or rerun migration 006: the Aiven restore and migration are already verified.

Vercel's Hobby plan is restricted to personal, non-commercial use. Review the appropriate company plan and spending limits before purchasing or deploying. See [Vercel plan guidance](https://vercel.com/docs/plans/hobby).

## What is prepared

- Separate fixed-role deployment entrypoints generated from the existing FastAPI factories.
- Selected frontend assets in each project's `public/assets`, with no employee records or private photos in public files.
- An explicit file allowlist that excludes private environments, SQL backups, local photos, virtual environments, tests, and administrative database commands from deployment uploads.
- Private Blob uploads and authenticated photo retrieval through the existing administrator routes.
- Verified Aiven TLS using the CA certificate supplied in `DB_SSL_CA_PEM`. Each function process writes only this certificate to a private temporary file; the laptop's certificate path is not used in the cloud.
- Secure HTTPS origins and cookies, with platform-provided client identity used for rate limits.
- A scanner API network restriction that requires approved office public IP addresses or CIDR ranges when scanning is enabled. The scanner has no staff login. A visitor's or employee's QR alone does not bypass this network restriction.
- Explicit email requests with background polling disabled. A single Send email action processes that employee's message. Bulk approval preserves the existing queue and then processes one approved message per HTTP request.
- A 4,000,000-byte photo limit displayed in the interface and enforced on the server. This leaves room below Vercel's documented [4.5 MB function payload limit](https://vercel.com/docs/functions/limitations).

Local startup remains unchanged. The Vercel entrypoints require Vercel's `VERCEL=1` environment marker and production settings; they are not general-purpose local proxy launchers.

## Remaining account and configuration decisions

1. The user created and verified the restricted `meal_runtime` account using [the prepared terminal command](aiven-runtime-account.md). Its credentials are stored separately in `var/private/vercel/database-runtime.env`, with owner-only permissions. The laptop environment files remain unchanged. Do not deploy using `avnadmin` or `root`.
2. The user created `mccia-meal-admin` and `mccia-meal-scanner` in `mccias-projects`. Read-only checks confirmed their assigned domains, `mccia-meal-admin.vercel.app` and `mccia-meal-scanner.vercel.app`; these are not yet deployed applications. Review costs before creating billable resources. Keep deployment protection enabled during setup and review access to the real employee data before publication.
3. Both **Production-only** connections to the existing **Private** `mccia-meal-photos` store are verified. Each includes `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_ID`, and `BLOB_WEBHOOK_PUBLIC_KEY` under their default names. Preserve the read/write token: the default OIDC settings alone do not satisfy the current application's explicit token requirement. The store Settings page confirms `BLOB_STORE_HOST=psdhbdpb2vxd56fq.private.blob.vercel-storage.com`. Do not create a duplicate store or substitute a public store. [Private Blob instructions](https://vercel.com/docs/vercel-blob/private-storage).
4. Obtain the office network's approved public outbound IP address or CIDR range for the scanner. An office LAN address such as `192.168.x.x` is not the address Vercel sees. Keep `SCAN_APP_ENABLED=false` until the actual access scope is approved. Office phones must use the approved network; mobile-data access requires its own suitable access design.
5. Review the two HTTPS origins, account credentials, existing QR keys, Gmail configuration, and Aiven CA certificate before deployment. Do not paste passwords or tokens into chat or command arguments.

The scanner checks Vercel's `x-vercel-forwarded-for` and `x-forwarded-proto` headers and refuses missing, malformed, or duplicated values. This trust is limited to the Vercel-specific entrypoints. Do not run them behind an unreviewed alternative proxy or enable a custom trusted proxy without rechecking client identity. [Vercel request headers](https://vercel.com/docs/headers/request-headers).

## Prepare and inspect the two upload folders

The optional Python dependency group `vercel` declares the official Blob SDK and HTTPX. On 2026-09-10 the user installed Vercel CLI 59.15.1 under `build/vercel-tools` and Python SDK 0.10.0 in `.venv`. Python dependency checks and the 51 Vercel unit tests passed. Installed SDK signatures and result fields match the storage adapter; live storage operations remain unverified. npm reported that one transitive dependency does not list the installed Node 25.6.1 as supported. CLI login and authenticated project listings succeed; deployment remains unverified. Additional dependency installation still requires approval. The frontend packaging step uses the project's already installed Node build dependencies.

From the project directory, build each package:

```sh
.venv/bin/python deploy/vercel/package.py admin
.venv/bin/python deploy/vercel/package.py scanner
```

These commands create `build/vercel/admin` and `build/vercel/scanner`. Each includes its own `app.py`, dependency declarations, Python version, `vercel.json`, checksum manifest, and only its own public assets. Packaging refuses to overwrite an existing directory. For a later revision, use `--output` with a new directory and review that package instead of editing a completed package.

Verify the packaged source and selected assets:

```sh
.venv/bin/python build/vercel/admin/verify_bundle.py
.venv/bin/python build/vercel/scanner/verify_bundle.py
```

Upload only the selected package directory. Do not deploy the whole working project directory, and do not copy `.env`, `.env.aiven`, backups, photos, certificates, or credentials into either package. The generated `.vercelignore` permits only explicit packaged files. Keep each package linked to its own Vercel project; a conflicting `MEAL_APPLICATION` setting is rejected.

Vercel installs the declared Python runtime dependencies during its build and runs the checksum verifier. Frontend assets are already compiled by local packaging, so the uploaded bundle does not need Node build dependencies. Review the package and exact project before authorizing a cloud deployment. Local package verification does not prove that Vercel's build and runtime will succeed. [FastAPI deployment behavior](https://vercel.com/docs/frameworks/backend/fastapi).

## Configure Vercel privately

The offline preparation command has created `var/private/vercel/environment-20260910`, an owner-only directory containing two private environment payloads and a sanitized `review.json`. It preserves the existing QR, CSRF, and login-rate secrets; uses the verified restricted database account; and includes the CA certificate and existing Gmail configuration. Each payload has 31 Production-only settings, including five sensitive settings. No Blob token is copied into these files: the existing Vercel connection supplies it. Provider access and token validity were not checked by offline preparation.

From the project directory, the user can upload this reviewed configuration with:

```sh
.venv/bin/python deploy/vercel/configure_projects.py --directory var/private/vercel/environment-20260910 --allow-cloud-settings
```

At the confirmation prompt, enter:

```text
CONFIGURE mccias-projects/mccia-meal-admin AND mccias-projects/mccia-meal-scanner PRODUCTION
```

This authorizes adding the prepared database, application, storage-host, and Gmail settings to the two named projects. The command verifies both project identities, domains, and existing environment metadata before the first write. It preserves the three Blob variables, refuses to overwrite existing settings, and verifies creation and readback without printing values. It does not deploy, connect to MySQL, upload photos, or send emails. Scanner access and email sending remain disabled.

The user's first upload attempt failed. A subsequent read-only check confirmed both projects still contained only their original three Blob variables; none of the application settings were created. Offline reproduction identified a Vercel CLI 59.15.1 transport issue: parsed JSON arrays are passed to fetch without JSON serialization. The uploader now supplies the reviewed JSON text through a JSON string envelope on standard input and explicitly sets the JSON content type. The CLI removes the envelope and transmits the original array text. The existing private payload files and checksums remain unchanged. The user's corrected run subsequently reported successful creation and metadata verification of all 31 application settings in both projects. This upload is complete; do not rerun the configuration command or recreate the database account.

The installed Vercel CLI may internally retry requests after network errors or server failures. The command never requests an upsert; a retry cannot intentionally replace an existing variable. If it reports a partial or uncertain result, stop and review the current project metadata before running anything again. Keep the private payload files intact, and do not paste their contents into chat. See the [environment creation API](https://vercel.com/docs/rest-api/projects/create-one-or-more-environment-variables).

Use `deploy/vercel/environment.example` as the names-and-placeholders checklist, not as a working secret file. The current shared runtime configuration requires Blob and Gmail settings in both projects, although the scanner exposes no photo administration or email APIs and never runs a sender. Reducing the scanner's unused provider configuration can be a separate hardening change.

Set the distinct final HTTPS values for `APP_ORIGIN` and `SCANNER_ORIGIN`, and include both exact hostnames in `ALLOWED_HOSTS`. Both projects use the administrator origin in `APP_ORIGIN`; the scanner factory selects `SCANNER_ORIGIN` internally. `MEAL_APPLICATION` is the only difference between the prepared payloads. Do not use wildcard hosts. Keep the origins stable once serving requests begin; scanner cookies are host-specific, so browser recovery cannot automatically cross to an unrelated deployment hostname.

Both projects must use the same Aiven host, port, database, restricted runtime account, and preserved QR encryption keys. Supply the downloaded Aiven CA certificate's full PEM text in `DB_SSL_CA_PEM`, including its certificate delimiters and actual newlines. The runtime verifies the CA and MySQL hostname. Do not copy the Mac's `DB_SSL_CA` filesystem path.

Set `BLOB_STORE_HOST` to the exact private hostname, for example `STORE_ID.private.blob.vercel-storage.com`, without a scheme or path. Set `BLOB_READ_WRITE_TOKEN` privately. Reads reject other hosts and redirects and return photo bytes only through authorized application routes. Keep `PHOTO_BACKEND=vercel_blob` in production; development local storage is still explicitly forbidden there.

Blob reads have an explicit 20-second transport timeout and a bounded byte buffer. SDK upload, metadata, and delete operations use the official SDK's timeout behavior; the supported SDK methods do not expose a per-call timeout setting. The overall Vercel function limit still applies. SDK installation and adapter signature compatibility are verified locally; live Blob operations remain unverified.

Set `EMAIL_PROCESS_LIMIT=1`, `EMAIL_AUTO_SEND_ENABLED=false`, and `EMAIL_SEND_ENABLED=false` during deployment verification. Set the Gmail sender and its App Password privately. Real sending remains a separate approval step. With this workflow, the administrator keeps the page open while approved bulk messages are processed; closing it leaves remaining approved messages available for later processing. A lost or uncertain send result requires status recovery and review, not automatic creation of a replacement email.

Do not attach the real Aiven credentials to untrusted pull-request preview deployments. Use protected, reviewed deployments for this data and separate disposable databases for destructive integration tests.

## Move the four existing photos before normal use

The database transfer preserved references to four files still on the laptop. It did not upload their contents. An approved photo transfer must read those existing references, upload the corresponding private files using the same opaque filenames to the new private Blob store, and verify every uploaded file before switching photo storage for live use. Keep the local files and backup intact. No photo transfer has run and no automated existing-photo migration command is included in this phase. Do not treat a missing cloud photo as an empty employee photo or overwrite the database references to hide a transfer failure.

## Verify and publish

The latest CLI administrator attempt failed with `VERCEL_BUNDLE_CONFIGURATION_MISMATCH` for `vercel.json`, manifest entry 45, despite canonical JSON comparison. The diagnostic does not identify the changed field or distinguish invalid JSON from different configuration content. Formatting and object-key ordering alone are accepted. Both projects' latest checked deployment states were Error. The new Git workflow is documented separately and does not claim to have identified the old cloud transformation.

The original upload-rule failure was separately reproduced and fixed: corrected rules select all 48 administrator files and 50 scanner files while excluding private settings and link metadata. Local package verification passes, but that did not resolve the later hosted configuration-check failure. Account creation and Production environment upload are complete and must not be repeated as part of build recovery.

The following commands describe the previous CLI workflow only. They are retained as reference, not retry instructions for the connected Git projects. Use the Git deployment instructions for the current setup.

For a newly prepared folder only, link the administrator upload folder to its verified project; then deploy it:

```sh
./build/vercel-tools/node_modules/.bin/vercel link --yes --project prj_1UVQJAYlGzaZE1ZpN2mZuMclHkMu --scope mccias-projects --cwd build/vercel/admin
./build/vercel-tools/node_modules/.bin/vercel deploy --prod --scope mccias-projects --cwd build/vercel/admin
```

After the administrator deployment succeeds, link the scanner folder only if it is newly prepared; then deploy it:

```sh
./build/vercel-tools/node_modules/.bin/vercel link --yes --project prj_hSsR4EOk1Kdve5YVOPBbcctT5Itj --scope mccias-projects --cwd build/vercel/scanner
./build/vercel-tools/node_modules/.bin/vercel deploy --prod --scope mccias-projects --cwd build/vercel/scanner
```

Linking records project metadata under the selected package's `.vercel` directory, updates its local `.gitignore`, and refreshes an OIDC token in its `.env.local`. Keep that generated file private. These files are excluded from deployment by the existing explicit upload allowlist. The repository's `.env` and `.env.aiven` are untouched. The link command does not deploy; the following `deploy --prod` command uploads the selected package, starts the hosted build, and assigns the production domain upon success. See [project linking](https://vercel.com/docs/cli/link) and [deployment commands](https://vercel.com/docs/cli/deploy).

The expected production origins after successful deployment are `https://mccia-meal-admin.vercel.app` and `https://mccia-meal-scanner.vercel.app`. At this setup stage the scanner remains disabled and emails cannot be sent. The four existing photos still need their approved private transfer. A successful code deployment alone does not establish that the application is ready for normal use.

After deployment, verify each `/health/live` and `/health/ready` and administrator login and authorization. Complete the approved photo transfer and scanner access configuration before normal use. Then verify protected photo retrieval, independent scanner access, employee and master QR flows, original-result replay after retries, and dashboard reports using approved test servings. Scanner writes must continue when the administrator deployment is unavailable.

Verify the Gmail request workflow separately with an approved recipient before enabling real sending generally. A bounded request can still time out or lose its response; review committed email status and uncertain delivery before retrying. Vercel's 300-second function limit is explicitly declared and does not guarantee delivery.

Check laptop and phone cameras using the actual approved HTTPS scanner origin. Confirm rejection from outside the approved scanner network, session isolation, no administrator APIs on the scanner project, and no private content in CDN caches. Only then provide the two verified application URLs to staff. Unit tests and packaging checks do not replace these hosted integration and device checks.
