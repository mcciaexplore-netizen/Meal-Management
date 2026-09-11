# Deploy the connected Git repository

Both existing Vercel projects are connected to `mcciaexplore-netizen/Meal-Management`, production branch `main`. They must use different application roots within that repository. The two deployments still share the existing Aiven database and business modules.

The Git build compiles the frontend from the lockfile and copies the existing runtime allowlist into the selected application root. It verifies generated payload files without importing the application, loading environment files, connecting to MySQL, or sending email. Runtime startup still requires the existing production configuration. Build success does not verify database access, Blob access, login, scanner access, or email delivery.

## Configure each existing project

Before pushing the deployment changes, open each project's Settings, then Build and Deployment. Set the following values:

| Setting | mccia-meal-admin | mccia-meal-scanner |
| --- | --- | --- |
| Root Directory | `apps/admin` | `apps/scanner` |
| Framework Preset | FastAPI | FastAPI |
| Include source files outside the Root Directory in the Build Step | Enabled | Enabled |
| Build Command override | Disabled; use the checked-in `vercel.json` | Disabled; use the checked-in `vercel.json` |
| Install Command override | Disabled; use automatic Python dependency installation | Disabled; use automatic Python dependency installation |
| Output Directory override | Disabled | Disabled |
| Production Branch | `main` | `main` |

Preserve the existing Production environment variables and private Blob connections. The administrator must retain `MEAL_APPLICATION=admin`; the scanner must retain `MEAL_APPLICATION=scanner`. Do not copy the local `.env` or `.env.aiven` files into either project folder or Git. Keep real credentials limited to reviewed Production deployments.

The scanner Production project requires a secret `SCANNER_ACTIVATION_SECRET`. A new browser enters the activation code once and receives a secure HttpOnly scanner cookie. The code is never embedded in frontend files or stored in browser storage. An activated browser can move between Wi-Fi and mobile-data networks. `SCANNER_ALLOWED_CIDRS` is retained only for configuration compatibility and no longer controls scanner API access.

The shared-source option allows the build to read `backend`, `frontend`, and `deploy/vercel` from the same checkout. The build command installs the frontend's locked dependencies with npm, then invokes `build_git.py` for the fixed application role. Vercel installs Python dependencies from that application's `pyproject.toml`. These are installation steps inside an explicitly initiated cloud build; the local verification commands below do not install dependencies.

The tracked `app.py` in each root fixes the application role and rejects a conflicting setting. The generated `backend`, `frontend`, `public`, manifest, and build lock stay ignored by Git. Changes to runtime dependencies must also update both application dependency declarations; the build refuses dependency drift.

FastAPI automatic static discovery is disabled in each application's `pyproject.toml` to avoid importing the runtime app during that discovery step. Selected public assets are still served from `public/assets` with the checked-in security headers. Application pages and authenticated APIs remain served by their own FastAPI application.

See [Vercel FastAPI builds](https://vercel.com/docs/frameworks/backend/fastapi), [shared monorepo source](https://vercel.com/docs/monorepos/monorepo-faq), and [build configuration](https://vercel.com/docs/builds/configure-a-build).

## Verify locally, then publish the reviewed commit

Before releasing the employee-contact and master-group changes, review the live migration ledger with approval. The current application requires migrations 001 through 009. Apply only pending migrations during an approved maintenance window: `008_employee_contact_details.sql` preserves existing employee rows while adding company and phone, and `009_master_qr_meal_allowances.sql` creates the visitor-group allowance table. Preserve all existing records and QR keys. A successful frontend build does not apply database migrations.

After migration 009 creates the table, the existing `meal_runtime` account needs `INSERT` and `UPDATE` on `defaultdb.master_qr_allocations`; its existing `SELECT` on `defaultdb.*` covers reads. Grant only those additional privileges through an approved database-administration step. Do not recreate the runtime account, regenerate its password, or rerun initial account provisioning to add these grants. Both deployments need the same migrated database before the new code serves traffic.

With the already installed project dependencies, run from the repository root:

```sh
.venv/bin/python deploy/vercel/build_git.py admin
.venv/bin/python deploy/vercel/build_git.py scanner
PYTHONPATH=backend .venv/bin/python -m unittest discover -s tests/unit
```

API tests are included in the unit suite and use injected services. They do not connect to MySQL. Review `git status` and the actual changes before committing. Include both application scaffolds, the shared Git builder, its reused packaging module, relevant tests, and these instructions. Do not force-add ignored build outputs or private files. A commit alone does not deploy; a push to the connected production branch can trigger both deployments, so configure both roots before pushing. Do not push unrelated work as part of this change.

After the reviewed commit is pushed, inspect Deployments in both existing projects. Confirm that each deployment uses the intended commit, root, and application role. A successful Git build reports preparation of the selected application; it does not run the old uploaded `python verify_bundle.py` command. Wait for each deployment to reach Ready before checking its application URL. Do not redeploy an older failed commit expecting it to contain the new files.

The old CLI bundles under `build/vercel` remain private local build outputs. Their configuration-check failure has not been diagnosed to a specific cloud transformation. Do not reuse those failed deployment commands for this Git workflow. The Git builder validates its generated runtime payload directly and does not compare Vercel's build-time `vercel.json` against the old local bundle manifest.

## Hosted verification still required

After both builds reach Ready, verify the application startup and health responses, approved database connectivity, administrator login and permissions, scanner-device activation, and absence of administrator API routes on the scanner. Keep real email sending disabled until its separate configuration and verification steps are complete. The four existing local photos still require their approved private-storage transfer.

Verify employee and master QR meals, retry recovery, dashboard visibility, and phone camera behavior using approved test data. Create a master group with five people in the admin application; five deliberate scanner requests must create five meals with the saved contact details, and a sixth must reject as expired. The scanner must collect no visitor form. Retry the fifth request with its original browser and UUID after exhaustion; it must return that same fifth meal without consuming another allowance. Check that completed approval/rejection animations return to the initial scanner screen after two seconds, while connection failures retain their unresolved request. Neither local payload checks nor unit/API tests establish that hosted integration is working.
