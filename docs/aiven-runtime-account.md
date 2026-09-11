# Create the Aiven application account from your terminal

The administrator and scanner need a dedicated database account before deployment. This command creates the reviewed `meal_runtime`@`%` account on the Aiven database selected in `.env.aiven`. The `%` scope allows authentication from any source host that can reach the existing Aiven service. A generated password and an encrypted connection are required.

The command grants `SELECT` on `defaultdb.*` and the exact table-specific `INSERT` and `UPDATE` privileges in [the account proposal](aiven-transfer.md#vercel-only-runtime-account-proposal). It grants no `DELETE`, schema modification, account management, or grant-option privileges. It does not change application records, run migrations, restart servers, alter private environment files, upload files to Vercel, or send emails.

## Run from the project terminal

Use the existing project virtual environment. No additional installation is needed. The command reads only the explicitly selected environment file, without substituting exported environment values. It uses the Aiven migration credentials already in that file; it does not ask for the laptop's MySQL root password.

```sh
.venv/bin/python manage.py --env-file .env.aiven create-aiven-runtime --allow-database-changes
```

Review the printed grants and target. For the approved current service, enter this exact text when prompted:

```text
CREATE meal_runtime@% ON mysql-a0846c8-mcciaexplore-0d76.a.aivencloud.com:27404/defaultdb
```

If a different target is displayed, stop and review the configuration. Do not type a database password at this confirmation prompt. The command generates the new password and never prints it.

## Successful completion

Success prints `Aiven account meal_runtime@% created and verified.` and the path to `var/private/vercel/database-runtime.env`. That file has owner-only permissions and sits in a private directory ignored by Git. It contains database connection settings only; it is not a replacement for a complete application environment file. Do not paste its contents into chat or commit it.

Before reporting success, the command verifies MySQL 8.4, UTC, the selected database, negotiated TLS with certificate and hostname verification, migrations 001–007 and their checksums, the exact runtime account, mandatory SSL, no active or mandatory roles, and exactly the approved grants. The runtime account needs INSERT on `employee_archives` and `meal_voids` for administrator removals. These read-only checks do not verify application writes, triggers, concurrent scans, email delivery, or hosted execution. Those checks remain separate.

The command refuses existing credentials files, unsafe paths, an insecure output directory, or a mismatched confirmation. Plain `CREATE USER` refuses an existing account without resetting its password or adding grants to it. Grants start only after account creation returns successfully.

## If the command fails

Account creation and grants are not one rollbackable transaction. The command saves and synchronizes a private `database-runtime.pending.env` recovery file before attempting account creation. Only after all checks succeed does it publish the final credentials filename. A failure or lost response can leave an account with some privileges and a pending file; it never automatically retries, changes an existing account, or drops an account.

Keep any pending and final credentials files private and intact. Do not rerun the command or delete those files to bypass a refusal. Share only the displayed error code, operation stage, and MySQL error number for review. The helper suppresses raw SQL, passwords, and connector error details.

The command has been prepared for manual execution. Creating the account does not populate the Vercel environment settings or deploy either application.
