# Deploy on one EC2 instance with Amazon RDS for MySQL

Both applications run on a single EC2 instance with Docker Compose. Caddy terminates HTTPS with automatic Let's Encrypt certificates and routes each hostname to its container. The database is Amazon RDS for MySQL 8.4 in the same VPC, not publicly accessible. Photos are stored in a private S3 bucket and email goes through Gmail.

## Prerequisites

- **EC2:**
  - Docker with the Compose plugin.
  - An Elastic IP.
  - Security group open for inbound 80 and 443 from anywhere, and 22 from administrators only.
  - An instance role allowed `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on the photo bucket.
  - Instance metadata hop limit set to **2**, so containers can reach the role credentials.
- **RDS:**
  - MySQL 8.4 with public access off.
  - Security group allowing 3306 only from the EC2 instance's security group.
  - A custom parameter group with `log_bin_trust_function_creators=1` and `require_secure_transport=ON`.
    - Without the first setting, the triggers in the migrations fail: the RDS master user has no SUPER privilege, and binary logging is on whenever automated backups are.
  - Leave `time_zone` at its UTC default.
- **DNS:** two hostnames pointing at the Elastic IP. Until real names exist, `admin.<a-b-c-d>.sslip.io` and `scan.<a-b-c-d>.sslip.io` work, where `a-b-c-d` is the Elastic IP with dashes.

## Database certificate

Download the regional RDS CA bundle into the project's private directory. Both containers mount it read-only at `/run/db-ca.pem`, and the app verifies both the certificate and the hostname.

```sh
mkdir -p var/private && chmod 700 var/private
curl -sS -o var/private/db-ca.pem https://truststore.pki.rds.amazonaws.com/eu-north-1/eu-north-1-bundle.pem
```

## Application settings

Generate `.env.docker` on the server. Secrets are created on the first run and kept on later runs. Never delete the file while QR codes are in use: a new `QR_ENCRYPTION_KEYS` makes existing QR codes unreadable.

```sh
python3 deploy/docker/make-env.py admin.example.com scan.example.com \
    --db-host <rds-endpoint> --db-name meal_management \
    --s3-bucket <bucket> --aws-region eu-north-1 \
    --gmail-from <existing env file with EMAIL_SENDER and GMAIL_APP_PASSWORD> \
    --out .env.docker
```

The file sets production mode, the RDS endpoint with the restricted `meal_runtime` account and a generated password, S3 photos and Gmail. Real email sending stays **off** until `--email-send-enabled` is passed in a later, approved run.

## Migrations, runtime account and first administrator

Run these from the server in one-off containers built from the application image. Use plain `docker run`, not `docker compose run`: the compose services load `.env.docker`, which uses the restricted account.

Put the RDS master credentials in a private file outside the project, for example `~/mccia-rds/rds-admin.env` with mode 600. Values are literal, with no quotes:

```
DB_ENGINE=mysql
DB_HOST=<rds-endpoint>
DB_PORT=3306
DB_NAME=meal_management
DB_USER=admin
DB_PASSWORD=<master password>
DB_CONNECT_TIMEOUT=10
DB_SSL_CA=/run/db-ca.pem
QR_ENCRYPTION_KEYS=<any valid Fernet key; migrations do not use it>
```

Shared options for every one-off container:

```sh
ONEOFF="--rm --user $(id -u):$(id -g) -v $PWD/var/private/db-ca.pem:/run/db-ca.pem:ro"
```

1. **Apply migrations** as the master user. Their triggers are owned by that user, so don't rename or drop it later.

   ```sh
   docker run $ONEOFF --env-file ~/mccia-rds/rds-admin.env meal-management:latest \
       python manage.py migrate --allow-database-changes
   ```

2. **Create `meal_runtime`** with the same privileges as `create-aiven-runtime`:
   - `SELECT` on the database, plus the table-specific `INSERT`, `UPDATE` and `DELETE` grants listed in `meal_management.runtime_account` (`DELETE` only on `staff_account_roles`);
   - `REQUIRE SSL`, with the account limited to the VPC range;
   - no schema, account or grant privileges.

   The script refuses an existing account and checks the result by logging in as `meal_runtime`. Run it again with `--verify-only` at any time.

   ```sh
   docker run $ONEOFF --env-file ~/mccia-rds/rds-admin.env \
       -v $PWD/.env.docker:/run/app.env:ro \
       -v $PWD/deploy/docker/rds-runtime-account.py:/run/rds-runtime-account.py:ro \
       meal-management:latest python /run/rds-runtime-account.py --runtime-env /run/app.env
   ```

3. **Create the first administrator.** This is interactive and runs as `meal_runtime`:

   ```sh
   docker run -it $ONEOFF --env-file .env.docker meal-management:latest \
       python manage.py bootstrap-admin --allow-database-access
   ```

## Start or update the applications

```sh
docker compose --env-file .env.docker up -d --build
docker compose --env-file .env.docker ps
```

Pass `--env-file .env.docker` so that Compose can fill in `ADMIN_HOST` and `SCANNER_HOST` for Caddy and the health checks. Afterwards, check `https://<admin host>/health/ready` and `https://<scanner host>/health/ready`. Both must return `{"status":"ready"}`, which confirms MySQL 8.4 and all migrations.

## Releases with a new migration

Apply the migration before the new image serves traffic: code that needs a migration reports `/health/ready` as not ready until it exists, while the previous image keeps reporting ready afterwards. That makes the previous image the rollback path.

1. Take an RDS snapshot.
2. Build the new image on the server, keeping the previous one tagged, for example `docker tag meal-management:latest meal-management:previous`, then `docker compose --env-file .env.docker build`.
3. Apply migrations as the master user with the new image, as in step 1 above.
4. Grant the runtime account any new table privileges as the master user, then verify the complete set with `rds-runtime-account.py --verify-only`.
5. `docker compose --env-file .env.docker up -d`, then check both `/health/ready` endpoints.

To roll back, retag the previous image as `meal-management:latest` and run `docker compose --env-file .env.docker up -d`. Migrations stay applied.

Migrations 010 (scanner phones) and 011 (settings guards), together with the settings and role-change release, need these grants before step 5:

```sql
GRANT INSERT, UPDATE ON `meal_management`.`scanner_phones` TO 'meal_runtime'@'172.31.%';
GRANT UPDATE ON `meal_management`.`departments` TO 'meal_runtime'@'172.31.%';
GRANT UPDATE ON `meal_management`.`meal_types` TO 'meal_runtime'@'172.31.%';
GRANT DELETE ON `meal_management`.`staff_account_roles` TO 'meal_runtime'@'172.31.%';
```

`rds-runtime-account.py --verify-only` from the new image then confirms the complete set, including the single `DELETE` grant.

Rolling back after 010 is safe for meals and reports, but the earlier image does not check scanner phone records, so revoked or expired phones with a signed cookie would scan again until the new image is redeployed.

Meals became permanent after migration 011. Remove the runtime account's ability to record meal removals as the master user, then rerun `--verify-only` from the new image:

```sql
REVOKE INSERT ON `meal_management`.`meal_voids` FROM 'meal_runtime'@'172.31.%';
```

### Meal types and waiter shifts (migrations 012 and 013)

Before step 5, grant:

```sql
GRANT INSERT, DELETE ON `meal_management`.`employee_meal_types` TO 'meal_runtime'@'172.31.%';
GRANT INSERT ON `meal_management`.`meal_type_corrections` TO 'meal_runtime'@'172.31.%';
GRANT INSERT, UPDATE ON `meal_management`.`waiter_shifts` TO 'meal_runtime'@'172.31.%';
GRANT UPDATE ON `meal_management`.`waiter_shift_settings` TO 'meal_runtime'@'172.31.%';
```

Add an S3 lifecycle rule on the photo bucket that expires objects with the prefix `shift-selfies/` after 90 days. Employee photos have no prefix and are not affected. The instance role's existing `s3:GetObject`, `s3:PutObject` and `s3:DeleteObject` on the bucket's objects cover the new prefix.

Optional settings in `.env.docker`: `WAITER_SHIFT_HOURS` (default 3, 1 to 12) and `MEAL_TYPE_LOCK_HOURS` (default 12, 0 to 48). The scanner's Caddy body limit is 2 MB for selfie uploads.

After 012, earlier images refuse every scan with `SCANNER_NOT_CONFIGURED`, because their only meal type, `SCANNER-MEAL`, is deactivated. When rolling back to such an image, also run as the master user:

```sql
UPDATE meal_types SET is_active = TRUE WHERE code = 'SCANNER-MEAL';
```

Earlier images then scan again and record `SCANNER-MEAL`; they do not check eligibility, the meal-type lock or shifts, and employees they register get no meal types.

Only after such a rollback, run this as the master user before the new image serves traffic again, in this order. Redeploying does not rerun migration 012. The first statement removes `SCANNER-MEAL` from the scanner's choices and from new employees' defaults. The second gives every active meal type to employees with none, such as those registered during the rollback, who would otherwise be refused for every meal type:

```sql
UPDATE meal_types SET is_active = FALSE WHERE code = 'SCANNER-MEAL';
INSERT INTO employee_meal_types (employee_id, meal_type_id, created_at)
SELECT e.id, t.id, UTC_TIMESTAMP(6)
FROM employees e CROSS JOIN meal_types t
WHERE t.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM employee_meal_types x WHERE x.employee_id = e.id);
```

### Visitor QR meal types (migration 014)

Before step 5, grant:

```sql
GRANT INSERT, UPDATE ON `meal_management`.`master_qr_meal_types` TO 'meal_runtime'@'172.31.%';
```

The migration gives every existing visitor QR its remaining count for each active meal type, so a QR with 3 meals left can afterwards serve up to 3 meals of each type.

Rolling back past 014 needs no SQL. Earlier images ignore `master_qr_meal_types` and read the visitor QR's summary on `master_qr_allocations`, which always holds the largest remaining count across its meal types. They then serve up to that many meals of any type and count them only on the summary. Visitor QRs they create get no meal types.

Only after such a rollback, run this as the master user before the new image serves traffic again. If the rollback also went past 012, run the two statements above first, so the new rows do not include `SCANNER-MEAL`. The first statement lowers each meal type's remaining count to the QR's remaining summary, so meals served by the earlier image are not served again. The second gives QRs created during the rollback their remaining count for each active meal type, as the migration did:

<!-- visitor-redeploy -->
```sql
UPDATE master_qr_meal_types t JOIN master_qr_allocations a ON a.qr_id = t.qr_id
SET t.exhausted_at = CASE
        WHEN GREATEST(t.meals_used, t.meal_limit - (a.meal_limit - a.meals_used)) >= t.meal_limit
        THEN COALESCE(t.exhausted_at, UTC_TIMESTAMP(6)) ELSE NULL END,
    t.meals_used = LEAST(t.meal_limit, GREATEST(t.meals_used, t.meal_limit - (a.meal_limit - a.meals_used)))
WHERE GREATEST(t.meals_used, t.meal_limit - (a.meal_limit - a.meals_used)) > t.meals_used;
INSERT INTO master_qr_meal_types (qr_id, meal_type_id, meal_limit, meals_used, exhausted_at, created_at)
SELECT a.qr_id, t.id, a.meal_limit, a.meals_used, a.exhausted_at, UTC_TIMESTAMP(6)
FROM master_qr_allocations a CROSS JOIN meal_types t
WHERE t.is_active = TRUE
  AND NOT EXISTS (SELECT 1 FROM master_qr_meal_types x WHERE x.qr_id = a.qr_id);
```

## Resetting test data

`manage.py reset-test-data` removes every test record before go-live and keeps staff accounts and roles, meal types, departments, locations, counters, the shared scanner settings, the canteen location and radius, `schema_migrations`, database grants and `QR_ENCRYPTION_KEYS`. It empties employees and their QRs and meal types, visitor master QRs with their allocations and per-type allowances, meals, servings, requests, scan attempts and receipts, meal type corrections, meal removals, waiter shifts, email drafts and batches, removed-employee records, development seed records, visitor authorizations, staff sessions, login rate limits and the audit log. Every scanner phone and unused code is revoked unless listed in `--keep-phones`; the rows are kept because they cannot be deleted.

It runs only as the RDS master user. Tables are emptied with `TRUNCATE TABLE`, which does not fire the delete-guard triggers, so no trigger is dropped or changed; the command compares every trigger (including its creation time), CHECK constraint, foreign key and the schema fingerprint before and after, and fails if anything differs. Each table's next ID is restored afterwards, so new records never reuse an old number; the audit log alone restarts, with a `TEST_DATA_RESET` event as entry 1 recording the operator, snapshot ID and removed row counts. `TRUNCATE` commits on its own, so the reset is not one transaction: if it stops part way, run the same command again. The state file in the output folder keeps the original next IDs and photo keys for that rerun.

Without `--confirm` it is a dry run: row counts for every table, each scanner phone and what will happen to it, S3 photo counts, bucket versioning if the role can read it, and the run order. Nothing changes. A real run needs `--confirm`, `--snapshot-id` and typing `WIPE meal_management ON <host>`. It refuses a different host or database, the runtime account, missing migrations, an operator who is not an active administrator, and phone IDs that are not active phones. S3 photos are deleted only with `--include-s3`, using the keys recorded in the database, and only keys in the application's photo formats.

Run order:

1. Take an RDS snapshot in the console and wait until it is available.
2. Stop the applications: `docker compose --env-file .env.docker stop admin scanner` (Caddy keeps running).
3. Dry run, then review the output.
4. Real run.
5. `docker compose --env-file .env.docker up -d`.
6. Health checks on both `/health/ready` endpoints.

```sh
mkdir -p ~/mccia-reset && chmod 700 ~/mccia-reset
ONEOFF="--rm --user $(id -u):$(id -g) -v $PWD/var/private/db-ca.pem:/run/db-ca.pem:ro -v $HOME/mccia-reset:/run/out"
RESET="python manage.py reset-test-data --expect-host <rds-endpoint> --expect-db meal_management \
    --operator-email <administrator email> --s3-bucket <bucket> --aws-region eu-north-1"
docker run $ONEOFF --env-file ~/mccia-rds/rds-admin.env meal-management:latest $RESET
docker run -it $ONEOFF --env-file ~/mccia-rds/rds-admin.env meal-management:latest $RESET \
    --confirm --snapshot-id <snapshot-id> --include-s3
```

Add `--keep-phones ID,ID` to both runs to keep specific phones activated. Afterwards everyone signs in again, scanner phones need new activation codes, and employees and visitor QRs are created again. If the bucket has versioning enabled, deleted photos remain as earlier versions until removed in the console. To undo a reset, restore the RDS snapshot.

## Changing hostnames

Point the new DNS names at the Elastic IP, then rerun `make-env.py` with the new hostnames and the same `--out` file. That keeps the secrets. Then run `docker compose --env-file .env.docker up -d`, and Caddy obtains the new certificates. Activated scanner browsers must activate again on the new scanner hostname.
