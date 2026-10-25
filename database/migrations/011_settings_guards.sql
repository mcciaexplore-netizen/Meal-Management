SET time_zone = '+00:00';

DELIMITER $$

CREATE TRIGGER shared_scanner_location_guard
BEFORE UPDATE ON locations
FOR EACH ROW
BEGIN
    IF ((OLD.is_active = 1 AND NEW.is_active = 0) OR NOT (NEW.code <=> OLD.code))
        AND EXISTS (
            SELECT 1 FROM scan_app_settings p JOIN scanner_devices d ON d.id = p.scanner_id
            WHERE d.location_id = OLD.id
        )
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'The shared scanner location cannot be deactivated or recoded';
    END IF;
END$$

CREATE TRIGGER shared_scanner_counter_guard
BEFORE UPDATE ON scanner_devices
FOR EACH ROW
BEGIN
    IF ((OLD.is_active = 1 AND NEW.is_active = 0) OR NOT (NEW.code <=> OLD.code)
            OR NOT (NEW.location_id <=> OLD.location_id))
        AND EXISTS (SELECT 1 FROM scan_app_settings WHERE scanner_id = OLD.id)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'The shared scanner counter cannot be deactivated or recoded';
    END IF;
END$$

CREATE TRIGGER shared_scanner_meal_type_guard
BEFORE UPDATE ON meal_types
FOR EACH ROW
BEGIN
    IF ((OLD.is_active = 1 AND NEW.is_active = 0) OR NOT (NEW.code <=> OLD.code))
        AND EXISTS (SELECT 1 FROM scan_app_settings WHERE meal_type_id = OLD.id)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'The shared scanner meal type cannot be deactivated or recoded';
    END IF;
END$$

CREATE TRIGGER shared_scanner_identity_guard
BEFORE UPDATE ON staff_accounts
FOR EACH ROW
BEGIN
    IF OLD.is_scanner = 1 AND (
        (OLD.is_active = 1 AND NEW.is_active = 0) OR NOT (NEW.email <=> OLD.email)
    ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'The shared scanner identity cannot be deactivated or changed';
    END IF;
END$$

DELIMITER ;
