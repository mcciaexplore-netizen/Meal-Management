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

## Changing hostnames

Point the new DNS names at the Elastic IP, then rerun `make-env.py` with the new hostnames and the same `--out` file. That keeps the secrets. Then run `docker compose --env-file .env.docker up -d`, and Caddy obtains the new certificates. Activated scanner browsers must activate again on the new scanner hostname.
