# Employee email delivery

Individual registration and bulk import use different delivery flows. Both require an authenticated administrator. The scanner exposes neither flow.

## One employee

Register the employee, open their details, and click **Send email**. Registration prepares their reusable QR and a private delivery draft but sends nothing. Clicking Send email approves only that draft and contacts the configured provider during the request. Individual drafts are shown in employee history, not the bulk approval queue.

The response is successful only after the provider accepts the message and the database commits the result. Provider acceptance does not prove inbox arrival. A double-click or retry uses the same email ID and cannot send an already-confirmed message again. After a connection error, use the status check for that same email. Do not create a new resend to resolve an uncertain result.

A deliberate resend uses the same active QR. An existing unsent single draft is reused, and unresolved claimed sends block creation of another send until reviewed. Revoked or expired QRs, inactive employees, outdated recipients, and reserved test addresses cannot be delivered.

## Bulk employees

Use the bulk import action on the Employees page. Upload or paste CSV with the header:

```csv
employee_code,full_name,email,department_id
TEST-EXAMPLE-01,Fictional Employee,employee@example.test,1
```

Replace the fictional row with reviewed records and use existing active department IDs shown by the application. A batch supports 1–100 employees; this is a batch size, not a limit on the total employee population. Preview and validate all rows before importing. The example address is intentionally blocked from real delivery.

Importing creates every employee, personal QR, and pending email in one transaction. If any row fails, no employee from that batch is committed. Retrying an uncertain import keeps the original caller-bound request UUID and payload. After a reload, provide the original file again so its fingerprint can be matched; only the recovery UUID and fingerprint are stored in the browser, not employee records. Do not clear recovery storage while an import outcome is unknown.

Open **Email queue**, review recipients, select pending entries, and click **Approve and send**. The server records the authenticated administrator and approval time before processing the selected emails in bounded requests. If processing is interrupted, refresh and resume the approved entries. Pending unapproved entries remain untouched. Approval does not guarantee every recipient will accept delivery; failures and uncertain results remain visible for review.

## Configuration and migration

Migration `006_employee_email_approval.sql` is required. It preserves existing email history, labels existing rows as legacy, and holds previously queued messages for explicit approval. New single drafts cannot be selected by the background dispatcher. Even the CLI worker refuses an email without a recorded approval.

For direct and browser-driven bulk delivery, configure Gmail or SES and enable `EMAIL_SEND_ENABLED` after approval. `EMAIL_AUTO_SEND_ENABLED` can remain false. If separately enabled, the admin's optional background dispatcher resumes only approved bulk or legacy entries. It never sends single drafts or pending approvals. Preview mode sends no real messages.

Apply the migration only after confirming the target database and reviewing a backup. Stop the old admin process during the migration so an older sender cannot process queued messages under the previous policy. Coordinate the application restart; do not leave old code writing to the updated schema. No migration, live message, or configuration flag is enabled by a source update.

## Results

| Status | Meaning |
| --- | --- |
| `DRAFT` | Single email is ready for its Send email action |
| `PENDING_APPROVAL` | Bulk or legacy email is waiting for administrator approval |
| `QUEUED` | Approval was recorded and delivery is awaiting processing or confirmation |
| `SENT` / `ALREADY_SENT` | Provider acceptance and acknowledgement commit are confirmed |
| `FAILED` | Review the reported failure before deliberately preparing another message |
| `NEEDS_REVIEW` | An interrupted claim or delivery has an uncertain result; automatic retries are blocked |
| `CANCELLED` | The message is no longer eligible for delivery |

The database stores claimed or uncertain attempts as `FAILED`; safe status APIs translate their internal markers to `NEEDS_REVIEW`. Internal claim tokens, QR credentials, and provider exceptions are never returned.

## Verification

Unit and API tests use fake database and email adapters. Separate MySQL integration tests verify constraints, concurrent approvals, bulk rollback, and duplicate import recovery only when explicitly enabled against a disposable database. They must never be pointed at the application database. Live Gmail and camera checks need separate authorization and manual verification.
