SET time_zone = '+00:00';

ALTER TABLE serving_requests
    ADD COLUMN scan_visitor_hash BINARY(32) NULL,
    ADD CONSTRAINT chk_request_scan_visitor_binding CHECK (
        scan_visitor_hash IS NULL OR scan_bound_at IS NOT NULL
    );

ALTER TABLE servings
    ADD COLUMN visitor_company_name VARCHAR(150) NULL,
    ADD COLUMN visitor_name VARCHAR(150) NULL,
    ADD COLUMN visitor_email VARCHAR(254) NULL,
    ADD COLUMN visitor_phone VARCHAR(32) NULL,
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
                    AND visitor_email IS NOT NULL AND visitor_phone IS NOT NULL
                    AND CHAR_LENGTH(TRIM(visitor_company_name)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_name)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_email)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_phone)) > 0
                )
            )
        )
    );

ALTER TABLE scan_attempts
    MODIFY COLUMN outcome ENUM('RECEIVED', 'SUCCESS', 'REJECTED', 'AWAITING_DETAILS') NOT NULL DEFAULT 'RECEIVED',
    DROP CHECK chk_scan_outcome,
    ADD CONSTRAINT chk_scan_outcome CHECK (
        (
            outcome = 'RECEIVED' AND completed_at IS NULL
            AND serving_id IS NULL AND rejection_code IS NULL
        ) OR (
            outcome = 'SUCCESS' AND completed_at IS NOT NULL
            AND serving_id IS NOT NULL AND staff_id IS NOT NULL
            AND scanner_id IS NOT NULL AND location_id IS NOT NULL
            AND rejection_code IS NULL
        ) OR (
            outcome = 'REJECTED' AND completed_at IS NOT NULL
            AND rejection_code IS NOT NULL AND CHAR_LENGTH(TRIM(rejection_code)) > 0
        ) OR (
            outcome = 'AWAITING_DETAILS' AND completed_at IS NOT NULL
            AND request_id IS NOT NULL AND qr_id IS NOT NULL
            AND staff_id IS NOT NULL AND scanner_id IS NOT NULL AND location_id IS NOT NULL
            AND serving_id IS NULL AND rejection_code IS NULL
        )
    );

DROP TRIGGER serving_request_guard;

DELIMITER $$

CREATE TRIGGER serving_request_guard
BEFORE UPDATE ON serving_requests
FOR EACH ROW
BEGIN
    IF NOT (NEW.id <=> OLD.id)
        OR NOT (NEW.payload_hash <=> OLD.payload_hash)
        OR NOT (NEW.created_at <=> OLD.created_at)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Request identity is immutable';
    END IF;
    IF (OLD.scan_bound_at IS NOT NULL OR OLD.status <> 'PENDING') AND (
        NOT (NEW.scan_authorization_id <=> OLD.scan_authorization_id)
        OR NOT (NEW.scan_visitor_hash <=> OLD.scan_visitor_hash)
        OR NOT (NEW.scan_bound_at <=> OLD.scan_bound_at)
    ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scan binding is immutable';
    END IF;
    IF OLD.status <> 'PENDING' AND (
        NOT (NEW.status <=> OLD.status)
        OR NOT (NEW.rejection_code <=> OLD.rejection_code)
        OR NOT (NEW.completed_at <=> OLD.completed_at)
    ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Finalized requests are immutable';
    END IF;
END$$

DELIMITER ;
