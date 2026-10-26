SET time_zone = '+00:00';

DROP TRIGGER shared_scanner_meal_type_guard;

INSERT INTO meal_types (code, name, is_active)
SELECT 'BREAKFAST', 'Breakfast', TRUE FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM meal_types WHERE code = 'BREAKFAST');

INSERT INTO meal_types (code, name, is_active)
SELECT 'LUNCH', 'Lunch', TRUE FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM meal_types WHERE code = 'LUNCH');

INSERT INTO meal_types (code, name, is_active)
SELECT 'HIGH-TEA', 'High Tea', TRUE FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM meal_types WHERE code = 'HIGH-TEA');

INSERT INTO meal_types (code, name, is_active)
SELECT 'DINNER', 'Dinner', TRUE FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM meal_types WHERE code = 'DINNER');

UPDATE meal_types SET is_active = FALSE WHERE code = 'SCANNER-MEAL';

CREATE TABLE employee_meal_types (
    employee_id BIGINT UNSIGNED NOT NULL,
    meal_type_id BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (employee_id, meal_type_id),
    KEY ix_employee_meal_types_type (meal_type_id),
    CONSTRAINT fk_employee_meal_type_employee
        FOREIGN KEY (employee_id) REFERENCES employees (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_employee_meal_type_type
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO employee_meal_types (employee_id, meal_type_id, created_at)
SELECT e.id, t.id, UTC_TIMESTAMP(6)
FROM employees e CROSS JOIN meal_types t
WHERE t.is_active = TRUE;

CREATE TABLE meal_type_corrections (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    meal_id BIGINT UNSIGNED NOT NULL,
    from_meal_type_id BIGINT UNSIGNED NOT NULL,
    to_meal_type_id BIGINT UNSIGNED NOT NULL,
    reason VARCHAR(255) NOT NULL,
    corrected_by_staff_id BIGINT UNSIGNED NOT NULL,
    corrected_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_meal_type_corrections_meal (meal_id, id),
    KEY ix_meal_type_corrections_staff (corrected_by_staff_id),
    KEY ix_meal_type_corrections_from (from_meal_type_id),
    KEY ix_meal_type_corrections_to (to_meal_type_id),
    CONSTRAINT fk_meal_type_correction_meal
        FOREIGN KEY (meal_id) REFERENCES meals (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_meal_type_correction_from
        FOREIGN KEY (from_meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_meal_type_correction_to
        FOREIGN KEY (to_meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_meal_type_correction_staff
        FOREIGN KEY (corrected_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_meal_type_correction CHECK (
        from_meal_type_id <> to_meal_type_id AND CHAR_LENGTH(TRIM(reason)) > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

DELIMITER $$

CREATE TRIGGER meal_type_corrections_no_update
BEFORE UPDATE ON meal_type_corrections
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meal type corrections are immutable';
END$$

CREATE TRIGGER meal_type_corrections_no_delete
BEFORE DELETE ON meal_type_corrections
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meal type corrections cannot be deleted';
END$$

DELIMITER ;
