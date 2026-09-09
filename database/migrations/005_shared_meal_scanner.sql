SET time_zone = '+00:00';

ALTER TABLE staff_accounts
    MODIFY COLUMN password_hash VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
    ADD COLUMN is_scanner BOOLEAN NOT NULL DEFAULT FALSE,
    DROP CHECK chk_staff_required,
    ADD CONSTRAINT chk_staff_required CHECK (
        CHAR_LENGTH(TRIM(display_name)) > 0
        AND CHAR_LENGTH(TRIM(email)) > 0
    ),
    ADD CONSTRAINT chk_staff_identity CHECK (
        (is_scanner = 0 AND password_hash IS NOT NULL AND CHAR_LENGTH(password_hash) > 0)
        OR (is_scanner = 1 AND password_hash IS NULL)
    ),
    ADD CONSTRAINT chk_staff_scanner CHECK (is_scanner IN (0, 1));

CREATE TABLE scan_app_settings (
    id TINYINT UNSIGNED NOT NULL,
    staff_id BIGINT UNSIGNED NOT NULL,
    scanner_id BIGINT UNSIGNED NOT NULL,
    meal_type_id BIGINT UNSIGNED NOT NULL,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_scan_app_settings_staff (staff_id),
    KEY ix_scan_app_settings_scanner (scanner_id),
    KEY ix_scan_app_settings_meal (meal_type_id),
    CONSTRAINT fk_scan_app_settings_staff
        FOREIGN KEY (staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_app_settings_scanner
        FOREIGN KEY (scanner_id) REFERENCES scanner_devices (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_app_settings_meal
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scan_app_settings_singleton CHECK (id = 1),
    CONSTRAINT chk_scan_app_settings_enabled CHECK (is_enabled IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE scan_app_requests (
    request_id BINARY(16) NOT NULL,
    browser_hash BINARY(32) NOT NULL,
    staff_id BIGINT UNSIGNED NOT NULL,
    scanner_id BIGINT UNSIGNED NOT NULL,
    meal_type_id BIGINT UNSIGNED NOT NULL,
    scanner_code VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (request_id),
    KEY ix_scan_app_requests_browser_created (browser_hash, created_at),
    KEY ix_scan_app_requests_staff (staff_id),
    KEY ix_scan_app_requests_scanner (scanner_id),
    KEY ix_scan_app_requests_meal (meal_type_id),
    CONSTRAINT fk_scan_app_requests_staff
        FOREIGN KEY (staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_app_requests_scanner
        FOREIGN KEY (scanner_id) REFERENCES scanner_devices (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_app_requests_meal
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scan_app_request_scanner CHECK (CHAR_LENGTH(TRIM(scanner_code)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

DELIMITER $$

CREATE TRIGGER staff_account_kind_guard
BEFORE UPDATE ON staff_accounts
FOR EACH ROW
BEGIN
    IF NEW.is_scanner <> OLD.is_scanner THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Staff account kind is immutable';
    END IF;
END$$

CREATE TRIGGER scanner_role_insert_guard
BEFORE INSERT ON staff_account_roles
FOR EACH ROW
BEGIN
    IF NEW.role_code <> 'WAITER'
        AND (SELECT is_scanner FROM staff_accounts WHERE id = NEW.staff_id) = 1
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner accounts require the waiter role';
    END IF;
END$$

CREATE TRIGGER scanner_role_update_guard
BEFORE UPDATE ON staff_account_roles
FOR EACH ROW
BEGIN
    IF NEW.role_code <> 'WAITER'
        AND (SELECT is_scanner FROM staff_accounts WHERE id = NEW.staff_id) = 1
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner accounts require the waiter role';
    END IF;
END$$

CREATE TRIGGER human_session_insert_guard
BEFORE INSERT ON staff_sessions
FOR EACH ROW
BEGIN
    IF (SELECT is_scanner FROM staff_accounts WHERE id = NEW.staff_id) = 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner accounts cannot have staff sessions';
    END IF;
END$$

CREATE TRIGGER human_session_update_guard
BEFORE UPDATE ON staff_sessions
FOR EACH ROW
BEGIN
    IF (SELECT is_scanner FROM staff_accounts WHERE id = NEW.staff_id) = 1 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner accounts cannot have staff sessions';
    END IF;
END$$

CREATE TRIGGER scan_app_request_update_guard
BEFORE UPDATE ON scan_app_requests
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner request attribution is immutable';
END$$

CREATE TRIGGER scan_app_request_delete_guard
BEFORE DELETE ON scan_app_requests
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scanner request attribution is immutable';
END$$

DELIMITER ;

INSERT INTO staff_accounts (display_name, email, password_hash, is_scanner, is_active)
VALUES ('Meal Scanner', 'meal-scanner@system.invalid', NULL, TRUE, TRUE);

INSERT INTO staff_account_roles (staff_id, role_code)
SELECT id, 'WAITER' FROM staff_accounts WHERE email = 'meal-scanner@system.invalid';

INSERT INTO locations (code, name, is_active)
VALUES ('SHARED-SCANNER', 'Meal Scanner', TRUE);

INSERT INTO scanner_devices (code, name, location_id, is_active)
SELECT 'SHARED-SCANNER', 'Meal Scanner', id, TRUE FROM locations WHERE code = 'SHARED-SCANNER';

INSERT INTO meal_types (code, name, is_active)
VALUES ('SCANNER-MEAL', 'Meal', TRUE);

INSERT INTO scan_app_settings (id, staff_id, scanner_id, meal_type_id, is_enabled)
SELECT 1, s.id, d.id, m.id, TRUE
FROM staff_accounts s
CROSS JOIN scanner_devices d
CROSS JOIN meal_types m
WHERE s.email = 'meal-scanner@system.invalid'
    AND d.code = 'SHARED-SCANNER' AND m.code = 'SCANNER-MEAL';
