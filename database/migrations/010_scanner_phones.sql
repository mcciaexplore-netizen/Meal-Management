SET time_zone = '+00:00';

CREATE TABLE scanner_phones (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    label VARCHAR(100) NOT NULL,
    source ENUM('CODE', 'SECRET', 'LEGACY') NOT NULL,
    code_hash BINARY(32) NULL,
    code_expires_at DATETIME(6) NULL,
    active_until DATETIME(6) NULL,
    browser_hash BINARY(32) NULL,
    activated_at DATETIME(6) NULL,
    created_by_staff_id BIGINT UNSIGNED NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    revoked_at DATETIME(6) NULL,
    revoked_by_staff_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_scanner_phones_code (code_hash),
    UNIQUE KEY uq_scanner_phones_browser (browser_hash),
    KEY ix_scanner_phones_status (revoked_at, active_until),
    KEY ix_scanner_phones_creator (created_by_staff_id),
    KEY ix_scanner_phones_revoker (revoked_by_staff_id),
    CONSTRAINT fk_scanner_phone_creator
        FOREIGN KEY (created_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scanner_phone_revoker
        FOREIGN KEY (revoked_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scanner_phone_label CHECK (CHAR_LENGTH(TRIM(label)) > 0),
    CONSTRAINT chk_scanner_phone_source CHECK (
        (source = 'CODE' AND code_hash IS NOT NULL AND code_expires_at IS NOT NULL
            AND active_until IS NOT NULL AND created_by_staff_id IS NOT NULL)
        OR (source = 'SECRET' AND code_hash IS NULL AND code_expires_at IS NULL
            AND active_until IS NOT NULL AND browser_hash IS NOT NULL)
        OR (source = 'LEGACY' AND code_hash IS NULL AND code_expires_at IS NULL
            AND active_until IS NULL AND browser_hash IS NOT NULL)
    ),
    CONSTRAINT chk_scanner_phone_activation CHECK ((browser_hash IS NULL) = (activated_at IS NULL)),
    CONSTRAINT chk_scanner_phone_revocation CHECK ((revoked_at IS NULL) = (revoked_by_staff_id IS NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

DELIMITER $$

CREATE TRIGGER scanner_phone_update_guard
BEFORE UPDATE ON scanner_phones
FOR EACH ROW
BEGIN
    IF NOT (NEW.label <=> OLD.label)
        OR NOT (NEW.source <=> OLD.source)
        OR NOT (NEW.code_hash <=> OLD.code_hash)
        OR NOT (NEW.code_expires_at <=> OLD.code_expires_at)
        OR NOT (NEW.active_until <=> OLD.active_until)
        OR NOT (NEW.created_by_staff_id <=> OLD.created_by_staff_id)
        OR NOT (NEW.created_at <=> OLD.created_at)
        OR (OLD.browser_hash IS NOT NULL AND NOT (NEW.browser_hash <=> OLD.browser_hash))
        OR (OLD.activated_at IS NOT NULL AND NOT (NEW.activated_at <=> OLD.activated_at))
        OR (OLD.revoked_at IS NOT NULL AND OLD.browser_hash IS NULL AND NEW.browser_hash IS NOT NULL)
        OR (OLD.revoked_at IS NOT NULL AND (
            NOT (NEW.revoked_at <=> OLD.revoked_at)
            OR NOT (NEW.revoked_by_staff_id <=> OLD.revoked_by_staff_id)
        ))
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner phone identity is immutable';
    END IF;
END$$

CREATE TRIGGER scanner_phone_delete_guard
BEFORE DELETE ON scanner_phones
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner phone history is immutable';
END$$

DELIMITER ;
