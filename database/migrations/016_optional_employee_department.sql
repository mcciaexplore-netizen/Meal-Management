SET time_zone = '+00:00';

ALTER TABLE employees
    MODIFY COLUMN department_id BIGINT UNSIGNED NULL;
