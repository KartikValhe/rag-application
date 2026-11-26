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

-- pgvector lives in the same schema as citext (found from pg_extension, never hardcoded).
DO $$
DECLARE
    ext_schema name;
BEGIN
    SELECT n.nspname INTO ext_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'citext';
    EXECUTE format('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA %I', ext_schema);
END $$;

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


-- Create app_worker role (document ingestion worker). It can only claim and update rows of
-- app.ingest_jobs and read tenant ids; migration 0008 grants exactly that and nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker LOGIN PASSWORD 'worker_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

-- Create app_chat_maint role (chat maintenance in the worker only: retention delete, stale-pending marking,
-- citation blanking). Migration 0013 grants it USAGE on schema app and exactly its chat grants, nothing else.
-- NO password here on purpose: set one outside the repo (ALTER ROLE app_chat_maint PASSWORD ...) when the
-- worker needs CHAT_MAINT_DATABASE_URL. Roles are cluster-wide, so this one statement serves both databases.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_chat_maint') THEN
        CREATE ROLE app_chat_maint LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

GRANT USAGE ON SCHEMA app TO app_tenant, app_platform, app_worker;
ALTER ROLE app_tenant SET search_path = app;
ALTER ROLE app_platform SET search_path = app;
ALTER ROLE app_worker SET search_path = app;
ALTER ROLE app_chat_maint SET search_path = app;

-- 3. Setup ragplatform_test (test db)
\connect ragplatform_test

CREATE SCHEMA IF NOT EXISTS app;
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA app;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA app;

-- pgvector lives in the same schema as citext (found from pg_extension, never hardcoded).
DO $$
DECLARE
    ext_schema name;
BEGIN
    SELECT n.nspname INTO ext_schema
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'citext';
    EXECUTE format('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA %I', ext_schema);
END $$;

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


-- Create app_worker role (document ingestion worker). It can only claim and update rows of
-- app.ingest_jobs and read tenant ids; migration 0008 grants exactly that and nothing else.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_worker') THEN
        CREATE ROLE app_worker LOGIN PASSWORD 'worker_password'
            NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

-- app_chat_maint (no password, see above; the IF NOT EXISTS makes this a no-op after the first section).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_chat_maint') THEN
        CREATE ROLE app_chat_maint LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END $$;

GRANT USAGE ON SCHEMA app TO app_tenant, app_platform, app_worker;
ALTER ROLE app_tenant SET search_path = app;
ALTER ROLE app_platform SET search_path = app;
ALTER ROLE app_worker SET search_path = app;
ALTER ROLE app_chat_maint SET search_path = app;

