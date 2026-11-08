-- init.sql: Runs on first startup of the PostgreSQL container
-- Creates databases, extensions, schemas, and least-privileged application roles.

-- 1. Create test database if not exists
SELECT 'CREATE DATABASE ragplatform_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'ragplatform_test')\gexec

-- 2. Setup ragplatform (main dev db)
\connect ragplatform

CREATE SCHEMA IF NOT EXISTS app;
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA app;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA app;

-- Create app_tenant role (used by FastAPI for tenant traffic - NOBYPASSRLS)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_tenant') THEN
        CREATE ROLE app_tenant LOGIN PASSWORD 'tenant_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

-- Create app_platform role (used for superadmin traffic - platform tables only)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_platform') THEN
        CREATE ROLE app_platform LOGIN PASSWORD 'platform_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

GRANT USAGE ON SCHEMA app TO app_tenant, app_platform;
ALTER ROLE app_tenant SET search_path = app;
ALTER ROLE app_platform SET search_path = app;

-- 3. Setup ragplatform_test (test db)
\connect ragplatform_test

CREATE SCHEMA IF NOT EXISTS app;
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA app;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA app;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_tenant') THEN
        CREATE ROLE app_tenant LOGIN PASSWORD 'tenant_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_platform') THEN
        CREATE ROLE app_platform LOGIN PASSWORD 'platform_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

GRANT USAGE ON SCHEMA app TO app_tenant, app_platform;
ALTER ROLE app_tenant SET search_path = app;
ALTER ROLE app_platform SET search_path = app;

