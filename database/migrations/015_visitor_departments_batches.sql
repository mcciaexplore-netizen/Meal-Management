SET time_zone = '+00:00';

CREATE TABLE visitor_qr_batches (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NOT NULL,
    request_hash BINARY(32) NOT NULL,
    created_by_staff_id BIGINT UNSIGNED NOT NULL,
    department_id BIGINT UNSIGNED NOT NULL,
    event_name VARCHAR(150) NULL,
    expires_at DATETIME(6) NOT NULL,
    meal_type_ids JSON NOT NULL,
    row_count SMALLINT UNSIGNED NOT NULL,
    source_name VARCHAR(255) NOT NULL,
    email_batch_id BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_visitor_batch_request (created_by_staff_id, request_id),
    KEY ix_visitor_batch_hash (created_by_staff_id, request_hash),
    KEY ix_visitor_batch_created (created_at, id),
    KEY ix_visitor_batch_department (department_id),
    KEY ix_visitor_batch_email (email_batch_id),
    CONSTRAINT fk_visitor_batch_creator
        FOREIGN KEY (created_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_visitor_batch_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_visitor_batch_email
        FOREIGN KEY (email_batch_id) REFERENCES employee_email_batches (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_visitor_batch_rows CHECK (row_count BETWEEN 1 AND 200),
    CONSTRAINT chk_visitor_batch_text CHECK (
        CHAR_LENGTH(TRIM(source_name)) > 0
        AND (event_name IS NULL OR CHAR_LENGTH(TRIM(event_name)) > 0)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE master_qr_allocations
    ADD COLUMN department_id BIGINT UNSIGNED NULL,
    ADD COLUMN event_name VARCHAR(150) NULL,
    ADD COLUMN batch_id BIGINT UNSIGNED NULL,
    ADD COLUMN batch_row SMALLINT UNSIGNED NULL,
    MODIFY COLUMN email VARCHAR(254) NULL,
    ADD KEY ix_master_allocation_department (department_id),
    ADD UNIQUE KEY uq_master_allocation_batch_row (batch_id, batch_row),
    ADD CONSTRAINT fk_master_allocation_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    ADD CONSTRAINT fk_master_allocation_batch
        FOREIGN KEY (batch_id) REFERENCES visitor_qr_batches (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    DROP CHECK chk_master_allocation_details,
    ADD CONSTRAINT chk_master_allocation_details CHECK (
        CHAR_LENGTH(TRIM(company_name)) > 0
        AND CHAR_LENGTH(TRIM(contact_name)) > 0
        AND (email IS NULL OR CHAR_LENGTH(TRIM(email)) > 0)
        AND CHAR_LENGTH(TRIM(phone)) > 0
        AND (event_name IS NULL OR CHAR_LENGTH(TRIM(event_name)) > 0)
    ),
    ADD CONSTRAINT chk_master_allocation_batch CHECK ((batch_id IS NULL) = (batch_row IS NULL));

ALTER TABLE servings
    DROP CHECK chk_serving_kind,
    ADD CONSTRAINT chk_serving_kind CHECK (
        (
            kind = 'EMPLOYEE' AND employee_id IS NOT NULL
            AND quantity = 1 AND authorization_id IS NULL
            AND visitor_company_name IS NULL AND visitor_name IS NULL
            AND visitor_email IS NULL AND visitor_phone IS NULL
        ) OR (
            kind = 'MASTER' AND employee_id IS NULL
            AND (
                (
                    quantity > 0 AND authorization_id IS NOT NULL
                    AND visitor_company_name IS NULL AND visitor_name IS NULL
                    AND visitor_email IS NULL AND visitor_phone IS NULL
                ) OR (
                    quantity = 1 AND authorization_id IS NULL
                    AND visitor_company_name IS NOT NULL AND visitor_name IS NOT NULL
                    AND visitor_phone IS NOT NULL
                    AND CHAR_LENGTH(TRIM(visitor_company_name)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_name)) > 0
                    AND (visitor_email IS NULL OR CHAR_LENGTH(TRIM(visitor_email)) > 0)
                    AND CHAR_LENGTH(TRIM(visitor_phone)) > 0
                )
            )
        )
    );

DELIMITER $$

CREATE TRIGGER visitor_qr_batches_no_update
BEFORE UPDATE ON visitor_qr_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Visitor QR batches are immutable';
END$$

CREATE TRIGGER visitor_qr_batches_no_delete
BEFORE DELETE ON visitor_qr_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Visitor QR batches cannot be deleted';
END$$

DELIMITER ;
