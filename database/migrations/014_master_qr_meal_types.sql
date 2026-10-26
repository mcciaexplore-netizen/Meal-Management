SET time_zone = '+00:00';

CREATE TABLE master_qr_meal_types (
    qr_id BIGINT UNSIGNED NOT NULL,
    meal_type_id BIGINT UNSIGNED NOT NULL,
    meal_limit SMALLINT UNSIGNED NOT NULL,
    meals_used SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    exhausted_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (qr_id, meal_type_id),
    KEY ix_master_qr_meal_types_type (meal_type_id),
    CONSTRAINT fk_master_qr_meal_type_allocation
        FOREIGN KEY (qr_id) REFERENCES master_qr_allocations (qr_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_master_qr_meal_type_type
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_master_qr_meal_type_usage CHECK (
        meal_limit > 0 AND meals_used <= meal_limit
    ),
    CONSTRAINT chk_master_qr_meal_type_exhaustion CHECK (
        (meals_used < meal_limit AND exhausted_at IS NULL)
        OR (meals_used = meal_limit AND exhausted_at IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO master_qr_meal_types (qr_id, meal_type_id, meal_limit, meals_used, exhausted_at, created_at)
SELECT a.qr_id, t.id, a.meal_limit, a.meals_used, a.exhausted_at, UTC_TIMESTAMP(6)
FROM master_qr_allocations a CROSS JOIN meal_types t
WHERE t.is_active = TRUE;

DELIMITER $$

CREATE TRIGGER master_qr_meal_type_update_guard
BEFORE UPDATE ON master_qr_meal_types
FOR EACH ROW
BEGIN
    IF NOT (NEW.qr_id <=> OLD.qr_id)
        OR NOT (NEW.meal_type_id <=> OLD.meal_type_id)
        OR NOT (NEW.meal_limit <=> OLD.meal_limit)
        OR NOT (NEW.created_at <=> OLD.created_at)
        OR NEW.meals_used < OLD.meals_used
        OR (OLD.exhausted_at IS NOT NULL AND NOT (NEW.exhausted_at <=> OLD.exhausted_at))
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Visitor QR meal type allowances can only be used, not changed';
    END IF;
END$$

CREATE TRIGGER master_qr_meal_type_delete_guard
BEFORE DELETE ON master_qr_meal_types
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Visitor QR meal type allowances cannot be deleted';
END$$

DELIMITER ;
