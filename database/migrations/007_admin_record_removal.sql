SET time_zone = '+00:00';

CREATE TABLE employee_archives (
    employee_id BIGINT UNSIGNED NOT NULL,
    archived_by_staff_id BIGINT UNSIGNED NOT NULL,
    reason VARCHAR(255) NOT NULL,
    archived_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (employee_id),
    KEY ix_employee_archives_actor_time (archived_by_staff_id, archived_at),
    KEY ix_employee_archives_time (archived_at),
    CONSTRAINT fk_employee_archives_employee
        FOREIGN KEY (employee_id) REFERENCES employees (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_employee_archives_actor
        FOREIGN KEY (archived_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_employee_archive_reason CHECK (
        CHAR_LENGTH(TRIM(reason)) > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE meal_voids (
    meal_id BIGINT UNSIGNED NOT NULL,
    voided_by_staff_id BIGINT UNSIGNED NOT NULL,
    reason VARCHAR(255) NOT NULL,
    voided_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (meal_id),
    KEY ix_meal_voids_actor_time (voided_by_staff_id, voided_at),
    KEY ix_meal_voids_time (voided_at),
    CONSTRAINT fk_meal_voids_meal
        FOREIGN KEY (meal_id) REFERENCES meals (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_meal_voids_actor
        FOREIGN KEY (voided_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_meal_void_reason CHECK (
        CHAR_LENGTH(TRIM(reason)) > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

DELIMITER $$

CREATE TRIGGER employee_archives_no_update
BEFORE UPDATE ON employee_archives
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee archives are immutable';
END$$

CREATE TRIGGER employee_archives_no_delete
BEFORE DELETE ON employee_archives
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee archives cannot be deleted';
END$$

CREATE TRIGGER meal_voids_no_update
BEFORE UPDATE ON meal_voids
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meal voids are immutable';
END$$

CREATE TRIGGER meal_voids_no_delete
BEFORE DELETE ON meal_voids
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meal voids cannot be deleted';
END$$

DELIMITER ;
