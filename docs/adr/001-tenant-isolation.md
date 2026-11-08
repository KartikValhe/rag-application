# ADR 001: Multi-Tenant Isolation Model

## Status
Accepted

## Context
The platform is a multi-tenant white-label academic and enterprise RAG chatbot. We need strict tenant isolation to guarantee that users in Organization A can never read, modify, or leak data belonging to Organization B, while keeping operational complexity and infrastructure costs sustainable as we scale to thousands of tenants.

We evaluated three architectural patterns:
1. **Shared schema + `tenant_id` + Postgres Row-Level Security (RLS)**: Single shared schema; all tenant tables carry `tenant_id`; RLS policies enforced at the PostgreSQL engine level; application connection sets `SET LOCAL app.tenant_id`.
2. **Schema-per-tenant**: Each tenant gets a dedicated PostgreSQL schema.
3. **Database-per-tenant**: Each tenant gets a separate PostgreSQL instance or database.

## Decision
We chose **Option 1: Shared schema + `tenant_id` + Postgres Row-Level Security (RLS)** with application-level filtering as defense in depth, paired with a nullable `tenants.db_dsn` routing escape hatch.

### Key Rules:
1. **Every tenant-scoped table** has `tenant_id NOT NULL` and an active RLS policy with `FORCE ROW LEVEL SECURITY`.
2. **No BYPASSRLS for API role**: The application connects using `app_tenant` which does not have `SUPERUSER` or `BYPASSRLS`.
3. **Per-transaction context**: Every transaction executes `SELECT set_config('app.tenant_id', :tid, true)` (`SET LOCAL`). Connections returning to the pool reset context automatically.
4. **Composite keys and foreign keys**: Primary keys and foreign keys use `(tenant_id, id)` to prevent cross-tenant foreign key references at the database schema level.
5. **Fail-closed**: Queries run without setting `app.tenant_id` return zero rows.
6. **Separate superadmin role**: Platform endpoints use `app_platform` to access only platform tables (`tenants`, `platform_admins`) and cannot read tenant tables.
7. **Automated isolation testing**: CI runs an automated test suite verifying cross-tenant SELECT, UPDATE, DELETE, and INSERT isolation across all tenant tables.

## Consequences
- Operations are lean: single database migration path, single connection pool.
- High assurance against data leakage.
- Dedicated DB escape hatch preserves support for large enterprise tenants without rewrites.

