# Gmail QR email setup

The admin application can automatically send queued employee QR emails through Gmail after registration or a QR resend. The email is queued in the same transaction as the registration or QR operation, and a background sender processes committed queue entries. Automatic delivery requires explicit configuration and approval. The sender runs inside the admin process; no additional application server or AWS account is required. The scanner process never starts email delivery.

The implementation uses `smtp.gmail.com` on port 465 with verified TLS and an App Password. Google documents this configuration for Gmail SMTP in [its application email setup guide](https://knowledge.workspace.google.com/admin/gmail/send-email-from-a-printer-scanner-or-app?hl=en).

## Configure your sending account

1. Sign in to the Google account that will send employee QR emails.
2. Enable 2-Step Verification for that account.
3. Open [Google App Passwords](https://myaccount.google.com/apppasswords) and create an entry named `Meal Management`.
4. Save the generated App Password privately. Put it only in the project's private `.env` or an approved secret store. Do not paste it into chat, terminal commands, screenshots, or `.env.example`.

[Google's App Password guidance](https://support.google.com/accounts/answer/185833?hl=en) explains the 2-Step Verification requirement and why App Passwords can be unavailable for some organization accounts, security-key-only configurations, or Advanced Protection. If the option is unavailable, ask the Google Workspace administrator about supported authentication. This implementation does not provide OAuth or an SMTP relay fallback. Changing the Google account password revokes existing App Passwords.

Update these values in the existing private `.env`, preserving the database, QR encryption, scanner, session, and photo settings:

| Setting | Value |
| --- | --- |
| `EMAIL_BACKEND` | `gmail` |
| `EMAIL_SENDER` | The complete Google account email address used to create the App Password |
| `GMAIL_APP_PASSWORD` | The generated 16-letter App Password; display spaces are accepted |
| `EMAIL_SEND_ENABLED` | Keep `false` while preparing configuration |
| `EMAIL_AUTO_SEND_ENABLED` | Keep `false` until ongoing automatic delivery is approved |
| `EMAIL_POLL_SECONDS` | `5`; allowed range 1–300 seconds |
| `EMAIL_BATCH_SIZE` | `10`; allowed range 1–100 messages per batch |

Keep local photo storage configured for development. Gmail does not require AWS settings. The Gmail username and sender address are the same account; custom sender aliases are not implemented.

From the project folder, validate configuration without a database or Gmail connection:

```sh
.venv/bin/python manage.py --env-file .env check-config
```

Both valid Gmail credentials and an enabled-sending flag are needed for delivery. Automatic delivery additionally requires `EMAIL_AUTO_SEND_ENABLED=true`; configuring automation with sending disabled or a preview backend fails validation. Configuration validation checks required settings and their format; it cannot establish that Google accepts an App Password. Existing shell environment variables take precedence over `.env`, so resolve conflicting exported settings if validation does not reflect the file.

## Enable automatic delivery

Obtain approval for ongoing real employee QR email delivery and queue-status updates in the intended development database before enabling these settings. The earlier one-message test approval does not authorize sending all future queued messages.

After approval, set both `EMAIL_SEND_ENABLED=true` and `EMAIL_AUTO_SEND_ENABLED=true` in the private `.env`, then restart the admin application with `sh deploy/start-admin.sh`. Restarting an already running process requires stopping that admin process first. Preserve the separate scanner process. The admin **Email queue** shows whether automatic delivery is configured and its sender is running.

The background sender checks for `QUEUED` entries about every five seconds by default, selects a bounded batch, and sends each message through the existing worker. It processes existing queued messages as well as future registrations and resends, so review the pending queue before enabling it. Cancelled, sent, failed, and uncertain attempts are not selected. A global authentication or sender-configuration failure stops automatic processing until the configuration is corrected and the admin server is restarted. Temporary database failures use bounded backoff.

Employee registration returns after its database commit without waiting for Gmail. A queued result is not a delivery confirmation. Refresh the email queue to see `SENT` after provider acceptance and successful acknowledgement commit. If the admin server is stopped, pending emails remain stored in MySQL and processing resumes after restart. The independent scanner can keep recording meals while administration is stopped.

On admin shutdown, the sender stops selecting new messages and allows the current attempt to finish. Shutdown has a bounded grace period; an unusually stuck database or provider call may outlive it, and an interrupted claimed entry then requires operator review. Set `EMAIL_AUTO_SEND_ENABLED=false` and restart the admin server to stop future automatic processing; also set `EMAIL_SEND_ENABLED=false` to disable manual delivery. Do not terminate or retransmit an uncertain attempt without checking its outcome.

## Review one message before sending

Open the administrator's **Email queue** and locate the intended queued message. Review its queue ID, employee, recipient address, and current QR. Development fixtures use reserved addresses and cannot be sent by this worker. Inactive employees, changed email addresses, revoked or expired QRs, and invalid encrypted payloads are rejected or cancelled before any provider call.

While in preview mode, **Preview** displays the local email. In Gmail mode the preview action is hidden. The employee's current QR remains available to authorized administrators. Queue status `QUEUED` means the message is waiting. With automatic delivery enabled, registering or choosing **Queue email resend** makes the new committed entry eligible for automatic processing.

For a manual one-message test, leave `EMAIL_AUTO_SEND_ENABLED=false` and separately approve the exact development database and recipient. Approval to edit code or select Gmail does not authorize a database connection or a real message. Enable `EMAIL_SEND_ENABLED=true` only when ready to perform the approved operation. The CLI rereads the environment file on each invocation; restart only the admin application when its displayed email mode needs to reflect changed configuration.

Set the selected queue ID, replacing the placeholder, and run the one-message command:

```sh
EMAIL_QUEUE_ID=REPLACE_WITH_REVIEWED_QUEUE_ID
.venv/bin/python manage.py --env-file .env send-email --email-id "$EMAIL_QUEUE_ID" --allow-database-changes --allow-real-email
```

The two CLI flags authorize queue-state changes in the configured database and a real external email for that selected queue entry. No other queue entry is selected by this command. Leave automatic delivery disabled for a single-message test so the admin background sender cannot independently select other queued entries. There is no browser send-now endpoint. No password is entered on the command line, and command output contains only safe identifiers, statuses, and error codes.

## Results and recovery

| Command status | Meaning and next action |
| --- | --- |
| `SENT` | The provider accepted the message and the application committed the acknowledgement. Check the recipient's inbox and spam folder; this is not proof of inbox delivery. |
| `ALREADY_SENT` | The queue entry was already recorded as sent. The command made no new provider call. |
| `CANCELLED` | Current employee or QR validation no longer permits delivery. Resolve the underlying issue and use the normal employee QR workflow. |
| `FAILED` | The attempt failed. Review the safe error and sender configuration before explicitly queuing a new resend. |
| `NEEDS_REVIEW` | A prior claim, interrupted send, or uncertain acknowledgement needs operator review. Check the sender's Sent folder and the recipient before considering a new resend. |

Successful or already-sent results exit with code 0; blocked, cancelled, failed, and uncertain outcomes do not. Refresh the dashboard after the command to see persisted queue status. The existing database enum uses `FAILED` for a reserved or uncertain delivery attempt, with a safe error marker; it does not mean that Google definitely rejected the message.

The worker commits its claim before contacting Gmail. Concurrent invocations cannot send the same queue entry. It rechecks the employee, QR, recipient, and token under database locks before sending, and holds those validity locks during the bounded SMTP attempt. It marks `SENT` only after provider acceptance and successful database commit. Interrupted claims are not automatically reclaimed, and failed entries are not automatically retried. An explicit resend from employee management creates a new queue entry for the same valid QR.

SMTP acceptance and a MySQL commit cannot be one atomic transaction. If the connection fails after Gmail may have accepted the message, or the acknowledgement commit is uncertain, automatic retransmission could duplicate the email. This implementation stops and requires review in those cases. Check delivery before creating a new resend; a new queue entry can intentionally send another copy.

## Verification and remaining work

Automated Gmail tests use injected SMTP clients and make no Gmail connections. Separate MySQL integration tests use a fake delivery adapter and require the project's existing disposable-database opt-ins. The existing schema is reused; no Gmail migration is needed.

An earlier explicitly approved one-message test was accepted by Gmail and recorded as `SENT` in the local database. Inbox arrival was not confirmed by that provider acknowledgement. Automatic delivery against a live database, MySQL concurrency, and crash recovery remain separate verification steps. Failed entries are not automatically retried. Delivery receipts, bounce or complaint ingestion, provider quota management, OAuth, and production operational verification remain future work.
