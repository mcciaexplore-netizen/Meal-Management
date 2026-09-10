SET time_zone = '+00:00';

CREATE TABLE employee_email_batches (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NOT NULL,
    created_by_staff_id BIGINT UNSIGNED NOT NULL,
    request_hash BINARY(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_employee_batch_request (created_by_staff_id, request_id),
    CONSTRAINT fk_employee_batch_creator FOREIGN KEY (created_by_staff_id)
        REFERENCES staff_accounts (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE email_queue
    MODIFY COLUMN status ENUM('QUEUED', 'CANCELLED', 'SENT', 'FAILED', 'DRAFT', 'PENDING_APPROVAL') NOT NULL DEFAULT 'DRAFT',
    ADD COLUMN delivery_mode ENUM('SINGLE', 'BULK', 'LEGACY') NOT NULL DEFAULT 'LEGACY',
    ADD COLUMN bulk_batch_id BIGINT UNSIGNED NULL,
    ADD COLUMN approved_by_staff_id BIGINT UNSIGNED NULL,
    ADD COLUMN approved_at DATETIME(6) NULL;

UPDATE email_queue SET status = 'PENDING_APPROVAL' WHERE status = 'QUEUED';

ALTER TABLE email_queue
    ALTER COLUMN delivery_mode SET DEFAULT 'SINGLE',
    ADD KEY ix_email_mode_status_created (delivery_mode, status, created_at),
    ADD KEY ix_email_bulk_batch (bulk_batch_id, id),
    ADD CONSTRAINT fk_email_bulk_batch FOREIGN KEY (bulk_batch_id)
        REFERENCES employee_email_batches (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    ADD CONSTRAINT fk_email_approved_staff FOREIGN KEY (approved_by_staff_id)
        REFERENCES staff_accounts (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    DROP CHECK chk_email_queued_payload,
    ADD CONSTRAINT chk_email_queued_payload CHECK (
        status NOT IN ('QUEUED', 'DRAFT', 'PENDING_APPROVAL') OR payload_ciphertext IS NOT NULL
    ),
    ADD CONSTRAINT chk_email_batch_mode CHECK (
        (delivery_mode = 'BULK' AND bulk_batch_id IS NOT NULL)
        OR (delivery_mode IN ('SINGLE', 'LEGACY') AND bulk_batch_id IS NULL)
    ),
    ADD CONSTRAINT chk_email_approval_pair CHECK (
        (approved_by_staff_id IS NULL AND approved_at IS NULL)
        OR (approved_by_staff_id IS NOT NULL AND approved_at IS NOT NULL AND approved_at >= created_at)
    ),
    ADD CONSTRAINT chk_email_approval_state CHECK (
        (status <> 'QUEUED' OR approved_by_staff_id IS NOT NULL)
        AND (status NOT IN ('DRAFT', 'PENDING_APPROVAL') OR approved_by_staff_id IS NULL)
        AND (status <> 'DRAFT' OR delivery_mode = 'SINGLE')
        AND (status <> 'PENDING_APPROVAL' OR delivery_mode IN ('BULK', 'LEGACY'))
    );

DELIMITER $$

CREATE TRIGGER employee_email_batch_update_guard
BEFORE UPDATE ON employee_email_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee email batch is immutable';
END$$

CREATE TRIGGER employee_email_batch_delete_guard
BEFORE DELETE ON employee_email_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee email batch is immutable';
END$$

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
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval attribution is immutable';
    END IF;
    IF OLD.approved_by_staff_id IS NULL AND NEW.approved_by_staff_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id
            WHERE s.id = NEW.approved_by_staff_id AND s.is_active = 1
                AND s.is_scanner = 0 AND r.role_code = 'ADMIN'
        ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval requires an administrator';
    END IF;
END$$

DELIMITER ;
