SET time_zone = '+00:00';

CREATE TABLE departments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    name VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_departments_name (name),
    CONSTRAINT chk_department_name CHECK (CHAR_LENGTH(TRIM(name)) > 0),
    CONSTRAINT chk_department_active CHECK (is_active IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE employees (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    employee_code VARCHAR(32) NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    company_name VARCHAR(150) NULL,
    phone VARCHAR(32) NULL,
    department_id BIGINT UNSIGNED NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    selfie_object_key VARCHAR(512) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_employees_code (employee_code),
    KEY ix_employees_department_active (department_id, is_active),
    KEY ix_employees_email (email),
    KEY ix_employees_company_name (company_name, full_name),
    CONSTRAINT fk_employees_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_employee_required CHECK (
        CHAR_LENGTH(TRIM(employee_code)) > 0
        AND CHAR_LENGTH(TRIM(full_name)) > 0
        AND CHAR_LENGTH(TRIM(email)) > 0
    ),
    CONSTRAINT chk_employee_active CHECK (is_active IN (0, 1)),
    CONSTRAINT chk_employee_selfie CHECK (
        selfie_object_key IS NULL OR CHAR_LENGTH(TRIM(selfie_object_key)) > 0
    ),
    CONSTRAINT chk_employee_company CHECK (
        company_name IS NULL OR CHAR_LENGTH(TRIM(company_name)) > 0
    ),
    CONSTRAINT chk_employee_phone CHECK (
        phone IS NULL OR CHAR_LENGTH(TRIM(phone)) > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE staff_accounts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    display_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
    is_scanner BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_staff_email (email),
    CONSTRAINT chk_staff_required CHECK (
        CHAR_LENGTH(TRIM(display_name)) > 0
        AND CHAR_LENGTH(TRIM(email)) > 0
    ),
    CONSTRAINT chk_staff_identity CHECK (
        (is_scanner = 0 AND password_hash IS NOT NULL AND CHAR_LENGTH(password_hash) > 0)
        OR (is_scanner = 1 AND password_hash IS NULL)
    ),
    CONSTRAINT chk_staff_scanner CHECK (is_scanner IN (0, 1)),
    CONSTRAINT chk_staff_active CHECK (is_active IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE roles (
    code VARCHAR(32) NOT NULL,
    PRIMARY KEY (code),
    CONSTRAINT chk_role_code CHECK (CHAR_LENGTH(TRIM(code)) > 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE staff_account_roles (
    staff_id BIGINT UNSIGNED NOT NULL,
    role_code VARCHAR(32) NOT NULL,
    PRIMARY KEY (staff_id, role_code),
    KEY ix_staff_roles_role (role_code),
    CONSTRAINT fk_staff_roles_account
        FOREIGN KEY (staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_staff_roles_role
        FOREIGN KEY (role_code) REFERENCES roles (code)
        ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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

CREATE TABLE system_locks (
    name VARCHAR(64) NOT NULL,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE staff_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    staff_id BIGINT UNSIGNED NOT NULL,
    token_hash BINARY(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    last_seen_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_session_token_hash (token_hash),
    KEY ix_sessions_staff_expiry (staff_id, expires_at),
    KEY ix_sessions_expiry (expires_at),
    CONSTRAINT fk_session_staff
        FOREIGN KEY (staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_session_expiry CHECK (expires_at > created_at),
    CONSTRAINT chk_session_revocation CHECK (
        revoked_at IS NULL OR revoked_at >= created_at
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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

CREATE TABLE locations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code VARCHAR(32) NOT NULL,
    name VARCHAR(150) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_locations_code (code),
    CONSTRAINT chk_location_required CHECK (
        CHAR_LENGTH(TRIM(code)) > 0 AND CHAR_LENGTH(TRIM(name)) > 0
    ),
    CONSTRAINT chk_location_active CHECK (is_active IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE scanner_devices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code VARCHAR(64) NOT NULL,
    name VARCHAR(150) NOT NULL,
    location_id BIGINT UNSIGNED NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_scanners_code (code),
    KEY ix_scanners_location (location_id),
    CONSTRAINT fk_scanner_location
        FOREIGN KEY (location_id) REFERENCES locations (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scanner_required CHECK (
        CHAR_LENGTH(TRIM(code)) > 0 AND CHAR_LENGTH(TRIM(name)) > 0
    ),
    CONSTRAINT chk_scanner_active CHECK (is_active IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE meal_types (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    code VARCHAR(32) NOT NULL,
    name VARCHAR(100) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (id),
    UNIQUE KEY uq_meal_types_code (code),
    CONSTRAINT chk_meal_type_required CHECK (
        CHAR_LENGTH(TRIM(code)) > 0 AND CHAR_LENGTH(TRIM(name)) > 0
    ),
    CONSTRAINT chk_meal_type_active CHECK (is_active IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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

CREATE TABLE qr_credentials (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    kind ENUM('EMPLOYEE', 'MASTER') NOT NULL,
    employee_id BIGINT UNSIGNED NULL,
    token_hash BINARY(32) NOT NULL,
    token_ciphertext BLOB NULL,
    issued_by BIGINT UNSIGNED NOT NULL,
    issued_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NULL,
    revoked_at DATETIME(6) NULL,
    revoked_by BIGINT UNSIGNED NULL,
    revocation_reason VARCHAR(255) NULL,
    unrevoked_employee_id BIGINT UNSIGNED GENERATED ALWAYS AS (
        CASE WHEN kind = 'EMPLOYEE' AND revoked_at IS NULL
            THEN employee_id ELSE NULL END
    ) STORED,
    PRIMARY KEY (id),
    UNIQUE KEY uq_qr_token_hash (token_hash),
    UNIQUE KEY uq_qr_id_kind (id, kind),
    UNIQUE KEY uq_qr_employee_kind (id, employee_id, kind),
    UNIQUE KEY uq_qr_unrevoked_employee (unrevoked_employee_id),
    KEY ix_qr_employee (employee_id),
    KEY ix_qr_expiry (expires_at),
    KEY ix_qr_issuer (issued_by),
    KEY ix_qr_revoker (revoked_by),
    CONSTRAINT fk_qr_employee
        FOREIGN KEY (employee_id) REFERENCES employees (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_qr_issuer
        FOREIGN KEY (issued_by) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_qr_revoker
        FOREIGN KEY (revoked_by) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_qr_owner CHECK (
        (kind = 'EMPLOYEE' AND employee_id IS NOT NULL)
        OR (kind = 'MASTER' AND employee_id IS NULL)
    ),
    CONSTRAINT chk_qr_expiry CHECK (
        expires_at IS NULL OR expires_at > issued_at
    ),
    CONSTRAINT chk_qr_revocation CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL)
        OR (
            revoked_at IS NOT NULL AND revoked_at >= issued_at
            AND revoked_by IS NOT NULL AND revocation_reason IS NOT NULL
            AND CHAR_LENGTH(TRIM(revocation_reason)) > 0
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

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

CREATE TABLE employee_email_batches (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NOT NULL,
    created_by_staff_id BIGINT UNSIGNED NOT NULL,
    request_hash BINARY(32) NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_employee_batch_request (created_by_staff_id, request_id),
    CONSTRAINT fk_employee_batch_creator FOREIGN KEY (created_by_staff_id)
        REFERENCES staff_accounts (id) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE email_queue (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    qr_id BIGINT UNSIGNED NOT NULL,
    recipient_email VARCHAR(254) NOT NULL,
    payload_ciphertext MEDIUMBLOB NULL,
    status ENUM('QUEUED', 'CANCELLED', 'SENT', 'FAILED', 'DRAFT', 'PENDING_APPROVAL') NOT NULL DEFAULT 'DRAFT',
    delivery_mode ENUM('SINGLE', 'BULK', 'LEGACY') NOT NULL DEFAULT 'SINGLE',
    bulk_batch_id BIGINT UNSIGNED NULL,
    approved_by_staff_id BIGINT UNSIGNED NULL,
    approved_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    last_error VARCHAR(1000) NULL,
    PRIMARY KEY (id),
    KEY ix_email_qr_status (qr_id, status),
    KEY ix_email_status_created (status, created_at),
    KEY ix_email_mode_status_created (delivery_mode, status, created_at),
    KEY ix_email_bulk_batch (bulk_batch_id, id),
    CONSTRAINT fk_email_bulk_batch FOREIGN KEY (bulk_batch_id)
        REFERENCES employee_email_batches (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_email_approved_staff FOREIGN KEY (approved_by_staff_id)
        REFERENCES staff_accounts (id) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_email_qr
        FOREIGN KEY (qr_id) REFERENCES qr_credentials (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_email_recipient CHECK (
        CHAR_LENGTH(TRIM(recipient_email)) > 0
    ),
    CONSTRAINT chk_email_queued_payload CHECK (
        status NOT IN ('QUEUED', 'DRAFT', 'PENDING_APPROVAL') OR payload_ciphertext IS NOT NULL
    ),
    CONSTRAINT chk_email_batch_mode CHECK (
        (delivery_mode = 'BULK' AND bulk_batch_id IS NOT NULL)
        OR (delivery_mode IN ('SINGLE', 'LEGACY') AND bulk_batch_id IS NULL)
    ),
    CONSTRAINT chk_email_approval_pair CHECK (
        (approved_by_staff_id IS NULL AND approved_at IS NULL)
        OR (approved_by_staff_id IS NOT NULL AND approved_at IS NOT NULL AND approved_at >= created_at)
    ),
    CONSTRAINT chk_email_approval_state CHECK (
        (status <> 'QUEUED' OR approved_by_staff_id IS NOT NULL)
        AND (status NOT IN ('DRAFT', 'PENDING_APPROVAL') OR approved_by_staff_id IS NULL)
        AND (status <> 'DRAFT' OR delivery_mode = 'SINGLE')
        AND (status <> 'PENDING_APPROVAL' OR delivery_mode IN ('BULK', 'LEGACY'))
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    actor_staff_id BIGINT UNSIGNED NULL,
    action VARCHAR(64) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(128) NOT NULL,
    before_data JSON NULL,
    after_data JSON NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY ix_audit_entity_time (entity_type, entity_id, created_at),
    KEY ix_audit_actor_time (actor_staff_id, created_at),
    KEY ix_audit_action_time (action, created_at),
    CONSTRAINT fk_audit_actor
        FOREIGN KEY (actor_staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_audit_required CHECK (
        CHAR_LENGTH(TRIM(action)) > 0
        AND CHAR_LENGTH(TRIM(entity_type)) > 0
        AND CHAR_LENGTH(TRIM(entity_id)) > 0
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE serving_requests (
    id BINARY(16) NOT NULL,
    payload_hash BINARY(32) NOT NULL,
    scan_authorization_id BIGINT UNSIGNED NULL,
    scan_visitor_hash BINARY(32) NULL,
    scan_bound_at DATETIME(6) NULL,
    status ENUM('PENDING', 'SUCCEEDED', 'REJECTED') NOT NULL DEFAULT 'PENDING',
    rejection_code VARCHAR(64) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    KEY ix_requests_status_created (status, created_at),
    CONSTRAINT chk_request_outcome CHECK (
        (status = 'PENDING' AND completed_at IS NULL AND rejection_code IS NULL)
        OR (status = 'SUCCEEDED' AND completed_at IS NOT NULL AND rejection_code IS NULL)
        OR (
            status = 'REJECTED' AND completed_at IS NOT NULL
            AND rejection_code IS NOT NULL AND CHAR_LENGTH(TRIM(rejection_code)) > 0
        )
    ),
    CONSTRAINT chk_request_time CHECK (
        completed_at IS NULL OR completed_at >= created_at
    ),
    CONSTRAINT chk_request_scan_visitor_binding CHECK (
        scan_visitor_hash IS NULL OR scan_bound_at IS NOT NULL
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE visitor_authorizations (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NOT NULL,
    qr_id BIGINT UNSIGNED NOT NULL,
    kind ENUM('EMPLOYEE', 'MASTER') NOT NULL DEFAULT 'MASTER',
    meal_type_id BIGINT UNSIGNED NOT NULL,
    location_id BIGINT UNSIGNED NOT NULL,
    quantity SMALLINT UNSIGNED NOT NULL,
    waiter_id BIGINT UNSIGNED NOT NULL,
    scanner_id BIGINT UNSIGNED NOT NULL,
    visitor_name VARCHAR(150) NOT NULL,
    visitor_organization VARCHAR(150) NULL,
    visit_purpose VARCHAR(255) NULL,
    authorized_by BIGINT UNSIGNED NOT NULL,
    authorized_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    expires_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_authorization_request (request_id),
    UNIQUE KEY uq_authorization_scope (
        id, request_id, qr_id, meal_type_id, location_id, quantity, waiter_id, scanner_id
    ),
    KEY ix_authorization_qr_kind (qr_id, kind),
    KEY ix_authorization_meal_type (meal_type_id),
    KEY ix_authorization_location (location_id),
    KEY ix_authorization_waiter (waiter_id),
    KEY ix_authorization_scanner (scanner_id),
    KEY ix_authorization_admin_time (authorized_by, authorized_at),
    CONSTRAINT fk_authorization_request
        FOREIGN KEY (request_id) REFERENCES serving_requests (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_qr
        FOREIGN KEY (qr_id, kind) REFERENCES qr_credentials (id, kind)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_meal_type
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_location
        FOREIGN KEY (location_id) REFERENCES locations (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_waiter
        FOREIGN KEY (waiter_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_scanner
        FOREIGN KEY (scanner_id) REFERENCES scanner_devices (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_authorization_admin
        FOREIGN KEY (authorized_by) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_authorization_kind CHECK (kind = 'MASTER'),
    CONSTRAINT chk_authorization_details CHECK (
        quantity > 0 AND CHAR_LENGTH(TRIM(visitor_name)) > 0
        AND expires_at > authorized_at
    ),
    CONSTRAINT chk_authorization_revocation CHECK (
        revoked_at IS NULL OR revoked_at >= authorized_at
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE servings (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NOT NULL,
    qr_id BIGINT UNSIGNED NOT NULL,
    kind ENUM('EMPLOYEE', 'MASTER') NOT NULL,
    employee_id BIGINT UNSIGNED NULL,
    meal_type_id BIGINT UNSIGNED NOT NULL,
    quantity SMALLINT UNSIGNED NOT NULL,
    waiter_id BIGINT UNSIGNED NOT NULL,
    scanner_id BIGINT UNSIGNED NOT NULL,
    location_id BIGINT UNSIGNED NOT NULL,
    authorization_id BIGINT UNSIGNED NULL,
    visitor_company_name VARCHAR(150) NULL,
    visitor_name VARCHAR(150) NULL,
    visitor_email VARCHAR(254) NULL,
    visitor_phone VARCHAR(32) NULL,
    served_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_serving_request (request_id),
    UNIQUE KEY uq_serving_authorization (authorization_id),
    UNIQUE KEY uq_serving_quantity (id, quantity),
    UNIQUE KEY uq_serving_attempt_scope (id, request_id, qr_id),
    KEY ix_serving_qr_kind (qr_id, kind),
    KEY ix_serving_qr_employee_kind (qr_id, employee_id, kind),
    KEY ix_serving_employee_time (employee_id, served_at),
    KEY ix_serving_qr_time (qr_id, served_at),
    KEY ix_serving_time_type (served_at, meal_type_id),
    KEY ix_serving_meal_type (meal_type_id),
    KEY ix_serving_waiter_time (waiter_id, served_at),
    KEY ix_serving_scanner (scanner_id),
    KEY ix_serving_location_time (location_id, served_at),
    KEY ix_serving_authorization_scope (
        authorization_id, request_id, qr_id, meal_type_id,
        location_id, quantity, waiter_id, scanner_id
    ),
    CONSTRAINT fk_serving_request
        FOREIGN KEY (request_id) REFERENCES serving_requests (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_qr_kind
        FOREIGN KEY (qr_id, kind) REFERENCES qr_credentials (id, kind)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_qr_employee_kind
        FOREIGN KEY (qr_id, employee_id, kind)
        REFERENCES qr_credentials (id, employee_id, kind)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_employee
        FOREIGN KEY (employee_id) REFERENCES employees (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_meal_type
        FOREIGN KEY (meal_type_id) REFERENCES meal_types (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_waiter
        FOREIGN KEY (waiter_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_scanner
        FOREIGN KEY (scanner_id) REFERENCES scanner_devices (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_location
        FOREIGN KEY (location_id) REFERENCES locations (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_serving_authorization_scope
        FOREIGN KEY (
            authorization_id, request_id, qr_id, meal_type_id,
            location_id, quantity, waiter_id, scanner_id
        ) REFERENCES visitor_authorizations (
            id, request_id, qr_id, meal_type_id,
            location_id, quantity, waiter_id, scanner_id
        ) ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_serving_kind CHECK (
        (
            kind = 'EMPLOYEE' AND employee_id IS NOT NULL
            AND quantity = 1 AND authorization_id IS NULL
            AND visitor_company_name IS NULL AND visitor_name IS NULL
            AND visitor_email IS NULL AND visitor_phone IS NULL
        ) OR (
            kind = 'MASTER' AND employee_id IS NULL
            AND (
                (
                    quantity > 0 AND authorization_id IS NOT NULL
                    AND visitor_company_name IS NULL AND visitor_name IS NULL
                    AND visitor_email IS NULL AND visitor_phone IS NULL
                ) OR (
                    quantity = 1 AND authorization_id IS NULL
                    AND visitor_company_name IS NOT NULL AND visitor_name IS NOT NULL
                    AND visitor_email IS NOT NULL AND visitor_phone IS NOT NULL
                    AND CHAR_LENGTH(TRIM(visitor_company_name)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_name)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_email)) > 0
                    AND CHAR_LENGTH(TRIM(visitor_phone)) > 0
                )
            )
        )
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE meals (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    serving_id BIGINT UNSIGNED NOT NULL,
    unit_number SMALLINT UNSIGNED NOT NULL,
    serving_quantity SMALLINT UNSIGNED NOT NULL,
    served_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_meal_serving_unit (serving_id, unit_number),
    KEY ix_meal_serving_quantity (serving_id, serving_quantity),
    KEY ix_meal_served_at (served_at),
    CONSTRAINT fk_meal_serving_quantity
        FOREIGN KEY (serving_id, serving_quantity) REFERENCES servings (id, quantity)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_meal_unit CHECK (
        serving_quantity > 0 AND unit_number > 0 AND unit_number <= serving_quantity
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

CREATE TABLE scan_attempts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    request_id BINARY(16) NULL,
    payload_hash BINARY(32) NULL,
    token_hash BINARY(32) NULL,
    qr_id BIGINT UNSIGNED NULL,
    staff_id BIGINT UNSIGNED NULL,
    scanner_id BIGINT UNSIGNED NULL,
    location_id BIGINT UNSIGNED NULL,
    reported_scanner_code VARCHAR(64) NULL,
    outcome ENUM('RECEIVED', 'SUCCESS', 'REJECTED', 'AWAITING_DETAILS') NOT NULL DEFAULT 'RECEIVED',
    serving_id BIGINT UNSIGNED NULL,
    rejection_code VARCHAR(64) NULL,
    received_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6) NULL,
    PRIMARY KEY (id),
    KEY ix_scan_request (request_id),
    KEY ix_scan_qr_time (qr_id, received_at),
    KEY ix_scan_outcome_time (outcome, received_at),
    KEY ix_scan_scanner_time (scanner_id, received_at),
    KEY ix_scan_staff_time (staff_id, received_at),
    KEY ix_scan_location_time (location_id, received_at),
    KEY ix_scan_serving_scope (serving_id, request_id, qr_id),
    CONSTRAINT fk_scan_request
        FOREIGN KEY (request_id) REFERENCES serving_requests (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_qr
        FOREIGN KEY (qr_id) REFERENCES qr_credentials (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_staff
        FOREIGN KEY (staff_id) REFERENCES staff_accounts (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_scanner
        FOREIGN KEY (scanner_id) REFERENCES scanner_devices (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_location
        FOREIGN KEY (location_id) REFERENCES locations (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT fk_scan_serving_scope
        FOREIGN KEY (serving_id, request_id, qr_id)
        REFERENCES servings (id, request_id, qr_id)
        ON DELETE RESTRICT ON UPDATE RESTRICT,
    CONSTRAINT chk_scan_serving_link CHECK (
        serving_id IS NULL OR (request_id IS NOT NULL AND qr_id IS NOT NULL)
    ),
    CONSTRAINT chk_scan_outcome CHECK (
        (
            outcome = 'RECEIVED' AND completed_at IS NULL
            AND serving_id IS NULL AND rejection_code IS NULL
        ) OR (
            outcome = 'SUCCESS' AND completed_at IS NOT NULL
            AND serving_id IS NOT NULL AND staff_id IS NOT NULL
            AND scanner_id IS NOT NULL AND location_id IS NOT NULL
            AND rejection_code IS NULL
        ) OR (
            outcome = 'REJECTED' AND completed_at IS NOT NULL
            AND rejection_code IS NOT NULL AND CHAR_LENGTH(TRIM(rejection_code)) > 0
        ) OR (
            outcome = 'AWAITING_DETAILS' AND completed_at IS NOT NULL
            AND request_id IS NOT NULL AND qr_id IS NOT NULL
            AND staff_id IS NOT NULL AND scanner_id IS NOT NULL AND location_id IS NOT NULL
            AND serving_id IS NULL AND rejection_code IS NULL
        )
    ),
    CONSTRAINT chk_scan_time CHECK (
        completed_at IS NULL OR completed_at >= received_at
    )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO roles (code) VALUES ('ADMIN'), ('WAITER'), ('AUDITOR');
INSERT INTO system_locks (name) VALUES ('bootstrap');

DELIMITER $$

CREATE TRIGGER qr_identity_immutable
BEFORE UPDATE ON qr_credentials
FOR EACH ROW
BEGIN
    IF NOT (NEW.id <=> OLD.id)
        OR NOT (NEW.kind <=> OLD.kind)
        OR NOT (NEW.employee_id <=> OLD.employee_id)
        OR NOT (NEW.token_hash <=> OLD.token_hash)
        OR NOT (NEW.issued_by <=> OLD.issued_by)
        OR NOT (NEW.issued_at <=> OLD.issued_at)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'QR identity is immutable';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND (
        NOT (NEW.revoked_at <=> OLD.revoked_at)
        OR NOT (NEW.revoked_by <=> OLD.revoked_by)
        OR NOT (NEW.revocation_reason <=> OLD.revocation_reason)
    ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'QR revocation is permanent';
    END IF;
END$$

CREATE TRIGGER qr_credentials_no_delete
BEFORE DELETE ON qr_credentials
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'QR credentials cannot be deleted';
END$$

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
        OR NOT (NEW.scan_visitor_hash <=> OLD.scan_visitor_hash)
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

CREATE TRIGGER serving_requests_no_delete
BEFORE DELETE ON serving_requests
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Serving requests cannot be deleted';
END$$

CREATE TRIGGER authorization_update_guard
BEFORE UPDATE ON visitor_authorizations
FOR EACH ROW
BEGIN
    IF NOT (NEW.id <=> OLD.id)
        OR NOT (NEW.request_id <=> OLD.request_id)
        OR NOT (NEW.qr_id <=> OLD.qr_id)
        OR NOT (NEW.kind <=> OLD.kind)
        OR NOT (NEW.meal_type_id <=> OLD.meal_type_id)
        OR NOT (NEW.location_id <=> OLD.location_id)
        OR NOT (NEW.quantity <=> OLD.quantity)
        OR NOT (NEW.waiter_id <=> OLD.waiter_id)
        OR NOT (NEW.scanner_id <=> OLD.scanner_id)
        OR NOT (NEW.visitor_name <=> OLD.visitor_name)
        OR NOT (NEW.visitor_organization <=> OLD.visitor_organization)
        OR NOT (NEW.visit_purpose <=> OLD.visit_purpose)
        OR NOT (NEW.authorized_by <=> OLD.authorized_by)
        OR NOT (NEW.authorized_at <=> OLD.authorized_at)
        OR NOT (NEW.expires_at <=> OLD.expires_at)
    THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Approval details are immutable';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND NOT (NEW.revoked_at <=> OLD.revoked_at) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Approval revocation is permanent';
    END IF;
END$$

CREATE TRIGGER authorizations_no_delete
BEFORE DELETE ON visitor_authorizations
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Approvals cannot be deleted';
END$$

CREATE TRIGGER servings_no_update
BEFORE UPDATE ON servings
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Servings are immutable';
END$$

CREATE TRIGGER servings_no_delete
BEFORE DELETE ON servings
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Servings cannot be deleted';
END$$

CREATE TRIGGER meals_no_update
BEFORE UPDATE ON meals
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meals are immutable';
END$$

CREATE TRIGGER meals_no_delete
BEFORE DELETE ON meals
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Meals cannot be deleted';
END$$

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

CREATE TRIGGER scan_attempt_update_guard
BEFORE UPDATE ON scan_attempts
FOR EACH ROW
BEGIN
    IF OLD.outcome <> 'RECEIVED' THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Finalized scan attempts are immutable';
    END IF;
    IF NOT (NEW.id <=> OLD.id) OR NOT (NEW.received_at <=> OLD.received_at) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scan receipt identity is immutable';
    END IF;
END$$

CREATE TRIGGER scan_attempts_no_delete
BEFORE DELETE ON scan_attempts
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Scan attempts cannot be deleted';
END$$

CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Audit events are immutable';
END$$

CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Audit events cannot be deleted';
END$$

DELIMITER ;

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

CREATE TRIGGER employee_email_batch_update_guard
BEFORE UPDATE ON employee_email_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee email batch is immutable';
END$$

CREATE TRIGGER employee_email_batch_delete_guard
BEFORE DELETE ON employee_email_batches
FOR EACH ROW
BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Employee email batch is immutable';
END$$

CREATE TRIGGER email_queue_approval_guard
BEFORE UPDATE ON email_queue
FOR EACH ROW
BEGIN
    IF NOT (NEW.id <=> OLD.id) OR NOT (NEW.qr_id <=> OLD.qr_id)
        OR NOT (NEW.recipient_email <=> OLD.recipient_email)
        OR NOT (NEW.created_at <=> OLD.created_at)
        OR NOT (NEW.delivery_mode <=> OLD.delivery_mode)
        OR NOT (NEW.bulk_batch_id <=> OLD.bulk_batch_id)
        OR (OLD.approved_by_staff_id IS NOT NULL AND (
            NOT (NEW.approved_by_staff_id <=> OLD.approved_by_staff_id)
            OR NOT (NEW.approved_at <=> OLD.approved_at)
        )) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval attribution is immutable';
    END IF;
    IF OLD.approved_by_staff_id IS NULL AND NEW.approved_by_staff_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM staff_accounts s JOIN staff_account_roles r ON r.staff_id = s.id
            WHERE s.id = NEW.approved_by_staff_id AND s.is_active = 1
                AND s.is_scanner = 0 AND r.role_code = 'ADMIN'
        ) THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Email approval requires an administrator';
    END IF;
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
