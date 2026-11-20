SET time_zone = '+00:00';

INSERT INTO roles (code) VALUES ('SUPER_ADMIN');

ALTER TABLE staff_accounts
    ADD COLUMN department_id BIGINT UNSIGNED NULL,
    ADD KEY ix_staff_accounts_department (department_id),
    ADD CONSTRAINT fk_staff_accounts_department
        FOREIGN KEY (department_id) REFERENCES departments (id)
        ON DELETE RESTRICT ON UPDATE RESTRICT;

UPDATE staff_account_roles r
JOIN staff_accounts s ON s.id = r.staff_id
SET r.role_code = 'SUPER_ADMIN'
WHERE s.email IN ('mcciaexplore@gmail.com', 'hr@autoclusterpune.org')
  AND r.role_code = 'ADMIN';
