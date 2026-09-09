SET time_zone = '+00:00';

CREATE TABLE development_seed_batches (
    seed_version VARCHAR(40) NOT NULL,
    created_by BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (seed_version),
    KEY ix_development_seed_batch_actor (created_by),
    CONSTRAINT fk_development_seed_batch_actor
        FOREIGN KEY (created_by) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE development_seed_records (
    seed_version VARCHAR(40) NOT NULL,
    fixture_key VARCHAR(40) NOT NULL,
    entity_type ENUM('EMPLOYEE', 'WAITER', 'MASTER', 'DEPARTMENT', 'LOCATION', 'MEAL_TYPE', 'SCANNER') NOT NULL,
    entity_id BIGINT UNSIGNED NOT NULL,
    label VARCHAR(150) NOT NULL,
    scenario ENUM('ACTIVE', 'INACTIVE', 'REVOKED', 'EXPIRED') NULL,
    expected_code VARCHAR(64) NULL,
    qr_id BIGINT UNSIGNED NULL,
    token_ciphertext BLOB NULL,
    created_by BIGINT UNSIGNED NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (seed_version, fixture_key),
    UNIQUE KEY uq_development_seed_entity (entity_type, entity_id),
    UNIQUE KEY uq_development_seed_qr (qr_id),
    KEY ix_development_seed_actor (created_by),
    CONSTRAINT fk_development_seed_batch
        FOREIGN KEY (seed_version) REFERENCES development_seed_batches (seed_version)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_development_seed_qr
        FOREIGN KEY (qr_id) REFERENCES qr_credentials (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_development_seed_actor
        FOREIGN KEY (created_by) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_development_seed_identity CHECK (
        entity_id > 0 AND CHAR_LENGTH(TRIM(fixture_key)) > 0 AND CHAR_LENGTH(TRIM(label)) > 0
    ),
    CONSTRAINT chk_development_seed_credentials CHECK (
        (entity_type IN ('EMPLOYEE', 'MASTER') AND qr_id IS NOT NULL AND token_ciphertext IS NOT NULL)
        OR (entity_type NOT IN ('EMPLOYEE', 'MASTER') AND qr_id IS NULL AND token_ciphertext IS NULL)
    ),
    CONSTRAINT chk_development_seed_scenario CHECK (
        (entity_type = 'EMPLOYEE' AND scenario IS NOT NULL AND expected_code IS NOT NULL)
        OR (entity_type <> 'EMPLOYEE' AND scenario IS NULL AND expected_code IS NULL)
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

DELIMITER $$

CREATE TRIGGER development_seed_batches_no_update
BEFORE UPDATE ON development_seed_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Development seed ownership is immutable';
END$$

CREATE TRIGGER development_seed_batches_no_delete
BEFORE DELETE ON development_seed_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Development seed ownership cannot be deleted';
END$$

CREATE TRIGGER development_seed_records_no_update
BEFORE UPDATE ON development_seed_records
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Development fixture ownership is immutable';
END$$

CREATE TRIGGER development_seed_records_no_delete
BEFORE DELETE ON development_seed_records
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Development fixture ownership cannot be deleted';
END$$

DELIMITER ;
