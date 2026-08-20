-- DataBridge AI v1.2.0 company_data_v2 evaluation fixture.
-- Source: https://github.com/idevelopAI/databridge-ai/tree/27b4a6ea96a8aec331afe758cc78dff50a1c6690
-- Run only in an empty, disposable PostgreSQL database.
-- The evaluation role and its credential are created outside this seed by CI.

BEGIN;

CREATE TABLE departments (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);

CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    salary DECIMAL(10, 2),
    department_id INT REFERENCES departments(id)
);

CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    budget DECIMAL(10, 2),
    department_id INT NOT NULL REFERENCES departments(id),
    status VARCHAR(20) NOT NULL CHECK (status IN ('planned', 'active', 'completed')),
    start_date DATE NOT NULL,
    end_date DATE
);

INSERT INTO departments (name) VALUES
('Sales'),
('Engineering'),
('HR'),
('Finance'),
('Marketing');

INSERT INTO employees (name, salary, department_id) VALUES
('Alice Smith', 75000, 1),
('Bob Jones', 120000, 2),
('Charlie Brown', 60000, 3),
('David Müller', 95000, 2),
('Eva Fischer', 110000, 4),
('Frank Wilson', 82000, 1),
('Grace Lee', 70000, 5),
('Hannah Weber', 105000, 2),
('Ian Carter', 65000, 3),
('Julia Klein', 98000, 4),
('Karl Schmidt', 88000, 5),
('Lea Martin', 78000, 1);

INSERT INTO projects (
    name,
    budget,
    department_id,
    status,
    start_date,
    end_date
) VALUES
('DataBridge AI', 150000, 2, 'active', '2026-06-01', NULL),
('Project Simon', 500000, 1, 'completed', '2025-01-15', '2025-12-15'),
('Cloud Migration', 300000, 2, 'active', '2026-01-10', '2026-11-30'),
('Hiring Portal', 90000, 3, 'planned', '2026-09-01', '2027-03-31'),
('Finance Automation', 220000, 4, 'active', '2026-03-01', '2026-10-31'),
('Brand Refresh', 130000, 5, 'completed', '2025-02-01', '2025-08-31');

COMMIT;
