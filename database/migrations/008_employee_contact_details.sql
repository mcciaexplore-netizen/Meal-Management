SET time_zone = '+00:00';

ALTER TABLE employees
    ADD COLUMN company_name VARCHAR(150) NULL AFTER email,
    ADD COLUMN phone VARCHAR(32) NULL AFTER company_name,
    ADD KEY ix_employees_company_name (company_name, full_name),
    ADD CONSTRAINT chk_employee_company CHECK (
        company_name IS NULL OR CHAR_LENGTH(TRIM(company_name)) > 0
    ),
    ADD CONSTRAINT chk_employee_phone CHECK (
        phone IS NULL OR CHAR_LENGTH(TRIM(phone)) > 0
    );
