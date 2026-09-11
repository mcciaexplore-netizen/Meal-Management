SET time_zone = '+00:00';

CREATE TABLE master_qr_allocations (
    qr_id BIGINT UNSIGNED NOT NULL,
    kind ENUM('EMPLOYEE', 'MASTER') NOT NULL DEFAULT 'MASTER',
    company_name VARCHAR(150) NOT NULL,
    contact_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    phone VARCHAR(32) NOT NULL,
    meal_limit SMALLINT UNSIGNED NOT NULL,
    meals_used SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    created_by_staff_id BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    exhausted_at DATETIME(6) NULL,
    PRIMARY KEY (qr_id),
    KEY ix_master_allocations_creator_time (created_by_staff_id, created_at),
    KEY ix_master_allocations_exhausted (exhausted_at, created_at),
    CONSTRAINT fk_master_allocation_qr_kind
        FOREIGN KEY (qr_id, kind) REFERENCES qr_credentials (id, kind)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_master_allocation_creator
        FOREIGN KEY (created_by_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_master_allocation_details CHECK (
        CHAR_LENGTH(TRIM(company_name)) > 0
        AND CHAR_LENGTH(TRIM(contact_name)) > 0
        AND CHAR_LENGTH(TRIM(email)) > 0
        AND CHAR_LENGTH(TRIM(phone)) > 0
    ),
    CONSTRAINT chk_master_allocation_usage CHECK (
        meal_limit > 0 AND meals_used <= meal_limit
    ),
    CONSTRAINT chk_master_allocation_kind CHECK (kind = 'MASTER'),
    CONSTRAINT chk_master_allocation_exhaustion CHECK (
        (meals_used < meal_limit AND exhausted_at IS NULL)
        OR (meals_used = meal_limit AND exhausted_at IS NOT NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
