-- Migration 017: SUPER_ADMIN role and staff departments.
-- Plain statements only: no DELIMITER, no stored procedures. Compound trigger
-- bodies keep every inner semicolon off the end of a line so the migration
-- runner sends the whole CREATE TRIGGER as one statement.
SET time_zone = '+00:00';

-- Guard before any change: if interactive staff exist, one of the two named
-- accounts must be an active ADMIN, or the migration stops here untouched.
CREATE TEMPORARY TABLE migration_017_guard (
    ok TINYINT NOT NULL,
    CONSTRAINT migration_017_requires_active_super_admin CHECK (ok = 1)
);

INSERT INTO migration_017_guard (ok)
SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM staff_accounts WHERE is_scanner = 0) THEN 1
    WHEN EXISTS (
        SELECT 1 FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id
        WHERE s.email IN ('mcciaexplore@gmail.com', 'hr@autoclusterpune.org')
            AND s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'ADMIN'
    ) THEN 1
    ELSE 0
END;

INSERT INTO roles (code) VALUES ('SUPER_ADMIN');

ALTER TABLE staff_accounts
    ADD COLUMN department_id BIGINT UNSIGNED NULL AFTER password_hash,
    ADD KEY ix_staff_accounts_department (department_id),
    ADD CONSTRAINT fk_staff_accounts_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;

-- Backfill by email only. Every other ADMIN stays ADMIN. No accounts are created.
UPDATE staff_account_roles r
JOIN staff_accounts s ON s.id = r.staff_id
SET r.role_code = 'SUPER_ADMIN'
WHERE s.email IN ('mcciaexplore@gmail.com', 'hr@autoclusterpune.org')
    AND s.is_scanner = 0
    AND r.role_code = 'ADMIN';

-- Same rule after the change: at least one active SUPER_ADMIN when staff exist.
INSERT INTO migration_017_guard (ok)
SELECT CASE
    WHEN NOT EXISTS (SELECT 1 FROM staff_accounts WHERE is_scanner = 0) THEN 1
    WHEN EXISTS (
        SELECT 1 FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id
        WHERE s.is_active = 1 AND s.is_scanner = 0 AND r.role_code = 'SUPER_ADMIN'
    ) THEN 1
    ELSE 0
END;

DROP TEMPORARY TABLE migration_017_guard;

-- email_queue_approval_guard (migration 006) hard-coded 'ADMIN'. Same body,
-- now also accepting SUPER_ADMIN as the approving administrator.
DROP TRIGGER email_queue_approval_guard;

CREATE TRIGGER email_queue_approval_guard
BEFORE UPDATE ON email_queue
FOR EACH ROW
BEGIN
    IF NOT (NEW.id <=> OLD.id) OR NOT (NEW.qr_id <=> OLD.qr_id)
        OR NOT (NEW.recipient_email <=> OLD.recipient_email)
        OR NOT (NEW.created_at <=> OLD.created_at)
        OR NOT (NEW.delivery_mode <=> OLD.delivery_mode)
        OR NOT (NEW.bulk_batch_id <=> OLD.bulk_batch_id)
        OR (OLD.approved_by_staff_id IS NOT NULL AND (
            NOT (NEW.approved_by_staff_id <=> OLD.approved_by_staff_id)
            OR NOT (NEW.approved_at <=> OLD.approved_at)
        )) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval attribution is immutable'
    ; END IF
    ; IF OLD.approved_by_staff_id IS NULL AND NEW.approved_by_staff_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id
            WHERE s.id = NEW.approved_by_staff_id AND s.is_active = 1
                AND s.is_scanner = 0 AND r.role_code IN ('ADMIN', 'SUPER_ADMIN')
        ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval requires an administrator'
    ; END IF
    ; END;
