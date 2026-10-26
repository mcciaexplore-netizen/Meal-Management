SET time_zone = '+00:00';

CREATE TABLE waiter_shift_settings (
    id TINYINT UNSIGNED NOT NULL,
    latitude DECIMAL(9, 6) NULL,
    longitude DECIMAL(9, 6) NULL,
    radius_m INT UNSIGNED NULL,
    updated_by_staff_id BIGINT UNSIGNED NULL,
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_waiter_shift_settings_staff (updated_by_staff_id),
    CONSTRAINT fk_waiter_shift_settings_staff
        FOREIGN KEY (updated_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_waiter_shift_settings_singleton CHECK (id = 1),
    CONSTRAINT chk_waiter_shift_settings_complete CHECK (
        (latitude IS NULL AND longitude IS NULL AND radius_m IS NULL)
        OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180 AND radius_m BETWEEN 10 AND 50000)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO waiter_shift_settings (id) VALUES (1);

CREATE TABLE waiter_shifts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    phone_id BIGINT UNSIGNED NOT NULL,
    waiter_name VARCHAR(150) NOT NULL,
    selfie_object_key VARCHAR(512) NOT NULL,
    latitude DECIMAL(9, 6) NOT NULL,
    longitude DECIMAL(9, 6) NOT NULL,
    accuracy_m DECIMAL(9, 1) NOT NULL,
    distance_m DECIMAL(10, 1) NULL,
    flagged BOOLEAN NULL,
    consent_notice_version VARCHAR(32) NOT NULL,
    consent_at DATETIME(6) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    expires_at DATETIME(6) NOT NULL,
    ended_at DATETIME(6) NULL,
    end_reason ENUM('SIGNED_OUT', 'REPLACED', 'ENDED_BY_ADMIN') NULL,
    ended_by_staff_id BIGINT UNSIGNED NULL,
    PRIMARY KEY (id),
    KEY ix_waiter_shifts_phone_open (phone_id, ended_at, expires_at),
    KEY ix_waiter_shifts_started (started_at),
    KEY ix_waiter_shifts_flagged (flagged, started_at),
    KEY ix_waiter_shifts_ended_by (ended_by_staff_id),
    CONSTRAINT fk_waiter_shift_phone
        FOREIGN KEY (phone_id) REFERENCES scanner_phones (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_waiter_shift_ended_by
        FOREIGN KEY (ended_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_waiter_shift_required CHECK (
        CHAR_LENGTH(TRIM(waiter_name)) > 0 AND CHAR_LENGTH(TRIM(selfie_object_key)) > 0
        AND CHAR_LENGTH(TRIM(consent_notice_version)) > 0
    ),
    CONSTRAINT chk_waiter_shift_location CHECK (
        latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180 AND accuracy_m >= 0
    ),
    CONSTRAINT chk_waiter_shift_flag CHECK ((distance_m IS NULL) = (flagged IS NULL)),
    CONSTRAINT chk_waiter_shift_time CHECK (consent_at <= started_at AND expires_at > started_at),
    CONSTRAINT chk_waiter_shift_end CHECK (
        (ended_at IS NULL AND end_reason IS NULL AND ended_by_staff_id IS NULL)
        OR (ended_at IS NOT NULL AND end_reason IS NOT NULL
            AND (end_reason = 'ENDED_BY_ADMIN') = (ended_by_staff_id IS NOT NULL))
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

ALTER TABLE servings
    ADD COLUMN waiter_shift_id BIGINT UNSIGNED NULL,
    ADD KEY ix_serving_waiter_shift (waiter_shift_id),
    ADD CONSTRAINT fk_serving_waiter_shift
        FOREIGN KEY (waiter_shift_id) REFERENCES waiter_shifts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;

ALTER TABLE scan_attempts
    ADD COLUMN waiter_shift_id BIGINT UNSIGNED NULL,
    ADD KEY ix_scan_waiter_shift (waiter_shift_id),
    ADD CONSTRAINT fk_scan_waiter_shift
        FOREIGN KEY (waiter_shift_id) REFERENCES waiter_shifts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;

DELIMITER $$

CREATE TRIGGER waiter_shift_update_guard
BEFORE UPDATE ON waiter_shifts
FOR EACH ROW
BEGIN
    IF NOT (NEW.phone_id <=> OLD.phone_id)
        OR NOT (NEW.waiter_name <=> OLD.waiter_name)
        OR NOT (NEW.selfie_object_key <=> OLD.selfie_object_key)
        OR NOT (NEW.latitude <=> OLD.latitude)
        OR NOT (NEW.longitude <=> OLD.longitude)
        OR NOT (NEW.accuracy_m <=> OLD.accuracy_m)
        OR NOT (NEW.distance_m <=> OLD.distance_m)
        OR NOT (NEW.flagged <=> OLD.flagged)
        OR NOT (NEW.consent_notice_version <=> OLD.consent_notice_version)
        OR NOT (NEW.consent_at <=> OLD.consent_at)
        OR NOT (NEW.started_at <=> OLD.started_at)
        OR NOT (NEW.expires_at <=> OLD.expires_at)
        OR (OLD.ended_at IS NOT NULL AND (
            NOT (NEW.ended_at <=> OLD.ended_at)
            OR NOT (NEW.end_reason <=> OLD.end_reason)
            OR NOT (NEW.ended_by_staff_id <=> OLD.ended_by_staff_id)
        ))
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Waiter shift records are immutable once ended';
    END IF;
END$$

CREATE TRIGGER waiter_shift_delete_guard
BEFORE DELETE ON waiter_shifts
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Waiter shift records cannot be deleted';
END$$

DELIMITER ;
