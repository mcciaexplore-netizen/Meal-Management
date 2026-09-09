SET time_zone = '+00:00';

ALTER TABLE staff_sessions
    ADD COLUMN last_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6);

UPDATE staff_sessions SET revoked_at = UTC_TIMESTAMP(6)
WHERE revoked_at IS NULL;

CREATE TABLE login_rate_limits (
    bucket_hash BINARY(32) NOT NULL,
    window_started_at DATETIME(6) NOT NULL,
    attempts INT UNSIGNED NOT NULL DEFAULT 0,
    blocked_until DATETIME(6) NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (bucket_hash),
    KEY ix_login_limits_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE serving_requests
    ADD COLUMN scan_authorization_id BIGINT UNSIGNED NULL,
    ADD COLUMN scan_bound_at DATETIME(6) NULL;

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
