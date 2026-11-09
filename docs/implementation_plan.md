# Implementation Plan: White-Label Multi-Tenant RAG Chatbot Platform

**Stack (fixed):** React + TypeScript · FastAPI · PostgreSQL **hosted on Supabase** · Redis (target architecture)
**Database hosting:** Supabase (managed PostgreSQL + pgvector + Storage). FastAPI stays the only door to the data. See section 1.6 for exactly what we use from Supabase and what we deliberately don't.
**Assumed team:** 3–4 engineers (2 backend, 1–2 frontend/full-stack). Estimates are in working weeks for that team size.
**Rough total:** ~28 weeks to a production-ready v1. A real pilot tenant can start around week 20 (end of Phase 7).

---

## 0. How to read this plan

Each phase has the same shape: **Goal → Components (Backend / Frontend / Data) → Exit criteria → Risks.**
Every phase ends with something you can demo and test, and each phase only depends on earlier ones.

Quick map:

| # | Phase | Weeks | You can demo... |
|---|-------|-------|-----------------|
| 1 | Tenancy core and identity | 4 | Superadmin creates an org + admin; admin invites users; tenants cannot see each other's data |
| 2 | RBAC and academic structure | 3 | Roles, classes, subjects, enrollments, teacher assignments |
| 3 | White-label theming | 2 | Each tenant's subdomain renders its own logo and colors |
| 4 | Timetables | 2 | Admin/teacher publishes a timetable; students view it |
| 5 | Document ingestion | 4 | Teacher uploads a PDF to a class + subject; it is parsed, chunked, embedded |
| 6 | Scoped RAG chat | 4 | Student chats with a bot grounded only in their class's material, with citations |
| 7 | Guardrails and evaluation | 4 | Inputs/outputs are checked; a quality score runs in CI |
| 8 | Redis hardening: caching, rate limits, quotas | 2 | Per-user/per-tenant limits, cached hot paths |
| 9 | Production readiness | 3 | Observability, audit, backups, deploy pipeline, load test |
| 10 | Optional differentiators | ongoing | Analytics, SSO, custom domains, etc. |

**Parallelism tip:** Phase 4 (timetables) is independent of Phase 5 (ingestion). If you have a second backend developer, run them side by side.

---

## 1. Key architectural decisions (read before Phase 1)

### 1.1 Tenant isolation model

| Option | How it works | Pros | Cons |
|--------|--------------|------|------|
| **A. Shared schema + `tenant_id` + Postgres Row-Level Security (RLS)** ← *recommended* | One set of tables. Every row has `tenant_id`. The database itself refuses to return other tenants' rows. | Cheap to run, one migration path, easy cross-tenant ops for superadmin, scales to thousands of tenants | A bug in the policy or a forgotten `tenant_id` can leak data; needs strong tests |
| B. Schema per tenant | Each tenant gets its own Postgres schema | Stronger separation, simple per-tenant backup | Migrations run N times; connection/catalog bloat past a few hundred tenants |
| C. Database per tenant | Each tenant has its own DB | Strongest isolation, easy to move or delete a tenant | Highest cost and ops burden; hard to pool connections |

**Decision:** Option A with RLS as the safety net, *plus* application-level filtering (defense in depth).
**Escape hatch:** keep a `tenants.db_dsn` / routing column from day one (nullable). If a large or regulated customer demands a dedicated DB later, route that tenant to its own database without a rewrite.

**Rules that make Option A safe:**
1. Every tenant-owned table has `tenant_id NOT NULL` and an RLS policy.
2. The app connects as a role that is **not** the table owner and does **not** have `BYPASSRLS`. Migrations use a separate owner role. (On Supabase, create these custom roles yourself; do not use the default `postgres` user for API traffic. See 1.6.)
3. The tenant is set **per transaction** with `SET LOCAL`, so a pooled connection can never carry a tenant over to the next request. (This also works with Supabase's Supavisor pooler in transaction mode, because `SET LOCAL` only lives inside one transaction.)
4. Child tables use **composite foreign keys** `(tenant_id, id)` so a row in tenant A can never point at a row in tenant B.
5. Superadmin endpoints use a **second, narrow DB role** that only touches platform tables (`tenants`, `platform_admins`, aggregate usage). Superadmin never reads tenant content.
6. A dedicated **cross-tenant leak test suite** runs in CI on every PR (see Phase 1 exit criteria).

**User identity decision:** a user belongs to exactly one tenant (`users.tenant_id`), and email is unique per tenant. The same human in two orgs has two accounts. Tradeoff: simpler and safer now; a future "one login, many orgs" feature would need an identity-linking layer. Superadmins live in a separate `platform_admins` table.

### 1.2 Vector store

| Option | Pros | Cons |
|--------|------|------|
| **pgvector inside the same PostgreSQL (Supabase supports the `vector` extension)** ← *recommended* | One datastore to run, back up, secure, and Supabase manages the server for you; RLS and joins apply to chunks too; transactional deletes (delete a document and its vectors together); hybrid search with Postgres full-text | Not as fast as dedicated engines at very large scale; filtered ANN needs care |
| Qdrant / Weaviate / Milvus | Built for filtered vector search at scale | A second system to operate, second place where tenant isolation must be enforced, sync issues with Postgres |
| Pinecone (managed) | No ops | Vendor lock-in, cost, tenant isolation via namespaces you must police |

**Decision:** pgvector with HNSW index. Reasoning: syllabus corpora are small (a class might have tens to hundreds of pages), so per-tenant, per-class filters make each search tiny.
**Mitigations for filtered search:** index `(tenant_id, class_id)`; enable pgvector's iterative index scan for filtered queries (pgvector 0.8+) so selective filters still return enough rows; consider list-partitioning `chunks` by `tenant_id` hash if a single table passes a few million rows.
**Abstraction:** hide retrieval behind a `Retriever` interface so a dedicated vector DB can be swapped in if you outgrow pgvector (trigger: sustained p95 retrieval > ~300 ms or > ~10M vectors).

### 1.3 How theming is stored and served

- **Stored** as *design tokens*, not free-form CSS: a `tenant_branding` row with JSON (`primary`, `secondary`, `background`, `surface`, `text`, `radius`, `font_family` from an allow-list), logo and favicon as object-storage keys, display name, tagline, support email, and a `version` integer.
- **Served** by a **public, unauthenticated** endpoint `GET /api/public/branding` that resolves the tenant from the request host (`{slug}.yourplatform.com`; custom domains in Phase 10). It returns tokens plus signed/CDN logo URLs.
- **Applied** by the React app at boot: fetch branding → write tokens as CSS variables on `:root` → render. Cache the response in Redis (server) and with `ETag`/`Cache-Control` (browser/CDN); bump `version` on edit to bust caches. Inline the last-known branding in `localStorage` to avoid a flash of default theme.
- **Why tokens, not custom CSS:** custom CSS lets a tenant break layouts, hide consent text, or inject script. Tokens give a consistent, accessible, safe UI. The admin editor validates **WCAG contrast** (text vs background) and rejects unreadable combos.
- **Tradeoff:** less pixel-perfect freedom than raw CSS. If a customer demands more, add an "advanced" layer later (sanitized CSS in a sandbox), not now.

### 1.4 Where guardrails and evaluation sit in the request path

```
Student message
   │
   ▼
[1] Rate limit + quota check (Redis)            ← cheap, fail fast
[2] Auth + tenant context + permission check     ← is this user enrolled in this class?
[3] INPUT GUARDRAILS                             ← length, PII, injection heuristics, moderation, off-topic
[4] Query rewrite (use chat history → standalone question)
[5] Retrieval (hybrid, filtered by tenant+class[+subject]) → rerank
[6] Context sufficiency check                    ← nothing relevant? → "I couldn't find this in your class material"
[7] Generation (streamed, context in delimiters, system prompt = tenant policy)
[8] OUTPUT GUARDRAILS                            ← citation present, no leaked system prompt, safety filter
[9] Persist message + citations + usage + audit
[10] ASYNC: sample N% of answers → LLM-judge scores → eval tables / dashboards
```

- **Guardrails are synchronous and cheap** (rules, regex, small classifier). Heavy LLM-based checks are either parallelized with step 4/5 or run **asynchronously** and flagged after the fact.
- **Evaluation is mostly offline + sampled online.** It never blocks a user. Offline golden sets gate releases in CI; online sampling catches drift.
- **Streaming tension:** you cannot fully check an output you are still streaming. Approach: stream with a rule-based filter on chunks, run groundedness checking post-hoc, and offer a non-streaming "strict mode" per tenant for sensitive deployments.

### 1.5 Other decisions (short)

| Topic | Decision | Tradeoff |
|-------|----------|----------|
| Async jobs | **ARQ** (Redis-backed, async-native, fits FastAPI). Alternative: Celery | ARQ is lighter but smaller ecosystem; Celery is heavier but battle-tested |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | Async SQLAlchemy has sharp edges with RLS session handling; wrap it in one helper |
| Auth | Short-lived JWT access token (10–15 min) + rotating refresh token (stored hashed, revocable via Redis/DB) | More moving parts than session cookies, but works for SPA + future mobile **Supabase Auth is deliberately not used in v1** (see 1.6). **Known trade-off:** Access tokens embed a roles and permissions snapshot; permission changes take up to 15 minutes (`ACCESS_TOKEN_EXPIRE_MINUTES`) to reach already-issued access tokens unless sessions are rotated or revoked. |
| LLM access | Provider-agnostic `LLMClient` + `Embedder` interfaces; model names in config | One more abstraction, but lets you change models/vendors and run offline eval cheaply |
| File storage | **Supabase Storage** (private buckets, accessed only by the backend; wrapped in an `ObjectStore` interface) | One less service to run, but plan limits (file size, storage) apply; the interface keeps S3/R2 as a drop-in alternative |
| API contract | FastAPI generates OpenAPI → `openapi-typescript` generates TS types/client | Needs a CI step, but removes a whole class of frontend/backend drift bugs |
| Open vs restricted tenant | `registration_mode`: `open` (anyone can self-register), `approval` (self-register, admin approves), `invite_only` | Note: **even in `open` mode, chat access still requires enrollment in a class.** Open only controls who can create an account |

### 1.6 Supabase: how it fits, and what we deliberately do not use

**In plain English:** Supabase is a hosted PostgreSQL database with extras bolted on (file storage, login system, auto-generated APIs, realtime updates). Because everything in this plan is *standard Postgres*, nothing in Phases 1-10 changes shape. What changes is who runs the server, and a few settings you must get right so Supabase's extras don't become a side door around your tenant isolation.

| Supabase feature | Used? | How / why |
|------------------|:-----:|-----------|
| PostgreSQL + `pgvector` + full-text search | **Yes** | Core of the plan. Enable the extension with `CREATE EXTENSION vector`; confirm the installed pgvector version before relying on iterative filtered scans (Phase 5/6) |
| Supavisor connection pooler | **Yes** | API uses the **transaction-mode** pooler URL; Alembic migrations and long-running worker jobs use the **direct** (or session-mode) URL |
| Storage | **Yes** | Private `documents` bucket (original uploads) and a public-read `branding` bucket (logos, favicons). Only the backend touches the private bucket, using the service key. Students and teachers receive short-lived signed URLs |
| Auth (Supabase's login system) | **No (v1)** | Supabase Auth keeps one global email per user, which conflicts with our "email unique per tenant" rule (section 1.1). Using it would mean switching to one global user with a `memberships(user_id, tenant_id, role)` table. That is a valid design, and it becomes attractive when SSO is requested (Phase 10), but it is a different Phase 1/2 data model. Decide *before* writing Phase 1 |
| Auto-generated REST/GraphQL API (PostgREST) | **No - lock it down** | Our frontend never talks to Supabase directly. See the security rules below |
| Realtime | **No (v1)** | Ingestion status uses polling/SSE from FastAPI so every update passes our auth. Realtime would go browser-to-Supabase and skip our checks |
| Edge Functions | **No** | Business logic lives in FastAPI |

**Security rules specific to Supabase (non-negotiable):**
1. Put all application tables in a private schema (`app`), **not** `public`, and keep `app` out of the Data API's exposed schemas (or disable the Data API entirely).
2. `REVOKE` all privileges on `app` from the `anon` and `authenticated` roles. RLS stays on as a second layer.
3. The frontend bundle contains **no** Supabase keys at all. The `service_role` key and database passwords exist only in backend/worker secrets. A leaked `service_role` key bypasses RLS completely.
4. Create custom roles for the app (the default `postgres` user is for admin work only):

```sql
-- Run once as the Supabase admin user (postgres)
CREATE SCHEMA IF NOT EXISTS app;                                   -- our tables live here, not in `public`
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions;      -- pgvector
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA extensions;      -- case-insensitive emails

-- Role for normal tenant requests: RLS is ALWAYS enforced for it
CREATE ROLE app_tenant   LOGIN PASSWORD '<from secrets manager>' NOSUPERUSER NOBYPASSRLS;
-- Narrow role for superadmin endpoints (platform tables only)
CREATE ROLE app_platform LOGIN PASSWORD '<from secrets manager>' NOSUPERUSER NOBYPASSRLS;

GRANT USAGE ON SCHEMA app, extensions TO app_tenant, app_platform; -- `extensions` so the vector type resolves
ALTER ROLE app_tenant   SET search_path = app, extensions;         -- unqualified names resolve to our schema
ALTER ROLE app_platform SET search_path = app, extensions;
REVOKE ALL ON SCHEMA app FROM anon, authenticated;                 -- Supabase's built-in API roles get nothing
-- Table-level GRANTs for each role are added by each Alembic migration
```

```python
# backend/app/db/engine.py
engine = create_async_engine(
    # Transaction-mode pooler URL (Supavisor), user is app_tenant (Supabase appends the project ref to pooler usernames)
    settings.DATABASE_URL,
    pool_size=5, max_overflow=5,        # keep our own pool small: the pooler already multiplexes connections
    connect_args={
        # In transaction mode each transaction may land on a different server connection,
        # so asyncpg's prepared-statement cache must be off or you get random "prepared statement does not exist" errors.
        "statement_cache_size": 0,
    },
)
# Also add ?prepared_statement_cache_size=0 to the URL for SQLAlchemy's own asyncpg cache.
# Alembic and long jobs use MIGRATION_DATABASE_URL (direct connection), never the transaction pooler.
```

**Practical notes**
- **One migration system:** keep **Alembic** as the single source of truth for schema. Don't also create tables by clicking in the Supabase dashboard or via the Supabase CLI's migration folder, or environments will drift.
- **Local vs hosted parity:** `supabase start` gives a local Postgres/Storage close to hosted Supabase. Run the full isolation suite against it in CI, then once against the staging Supabase project before each release.
- **Plans and limits:** treat the free tier as development-only (idle projects can pause, backups and storage are limited). Run the pilot on a paid plan, enable point-in-time recovery, and check current limits (database size, connections, file size) at supabase.com/pricing before sizing quotas in Phase 8.
- **Region is permanent:** choose it in Phase 1 (data residency and latency), and host the API and workers in the same region.
- **Lock-in is low if you stay disciplined:** we only use standard SQL plus Storage behind an `ObjectStore` interface. Moving off Supabase later means `pg_dump`/restore plus copying a bucket.
- **Redis is still separate:** Supabase does not provide Redis, so rate limits, caches, and the ARQ queue stay on a managed Redis (Phase 1 and Phase 8 unchanged).

---

## 2. Target architecture (end state)

```
Browser (React + TS, tenant theme via CSS variables)
   │  HTTPS (slug.platform.com)
   ▼
CDN / Reverse proxy (TLS, static assets, logo caching)
   ▼
FastAPI API (stateless, N replicas) ──────────► Redis  (cache, rate limits, job queue, pub/sub)
   │                                              ▲
   ├──► Supabase PostgreSQL (RLS, pgvector, tsvector) via Supavisor pooler │
   ├──► Supabase Storage (documents, logos)       │
   ├──► LLM / Embedding provider                  │
   ▼                                              │
ARQ Workers (ingestion, re-index, eval, exports) ─┘
   │
Observability: OpenTelemetry → traces/metrics/logs, Sentry
```

Suggested repo layout (monorepo):

```
/backend
  /app
    /api          # routers, one file per resource
    /core         # config, security, tenant context, logging
    /db           # models, session helpers, migrations (alembic)
    /services     # business logic (ingestion, retrieval, guardrails, eval)
    /workers      # ARQ tasks
  /tests          # unit, integration, isolation, eval
/frontend
  /src
    /features     # auth, admin, academics, timetable, documents, chat
    /theme        # token loader, ThemeProvider
    /api          # generated client
/infra            # docker-compose, Dockerfiles, deploy manifests
/docs/adr         # one short file per architectural decision
```

---

# PHASE 1: Tenancy core and identity (4 weeks)

## Goal
Stand up the walking skeleton and prove **strict tenant isolation** and **provisioning** before any feature depends on them. This is the foundation, so it gets the most careful testing.

## Components

### Backend
- **Project setup:** FastAPI app, Pydantic v2 settings, structured logging with `request_id` and `tenant_id`, Dockerfile, `docker-compose` for Redis and Mailhog, plus the **Supabase CLI local stack** (`supabase start`: Postgres with pgvector, Storage, Studio) in place of a hand-built Postgres/MinIO setup. Create a staging Supabase project now (not just local). CI running lint, type-check (mypy/pyright), tests.
- **Tenant resolution middleware:** extract tenant from host (`slug.platform.com`) for public/login routes; from JWT claims for authenticated routes. Reject mismatch (token for tenant A used on tenant B's host).
- **Auth:** register/login/refresh/logout, argon2 password hashing, email verification and password reset (console email backend in dev), login throttling via Redis counters (first Redis use).
- **Superadmin API:** separate login (`platform_admins`), create/suspend/archive tenant, create the first tenant admin (invite link or temp password flow).
- **Tenant admin API:** invite user (email + role placeholder), list/deactivate users, set `registration_mode`, approve/reject pending registrations, **bulk CSV import** of users (essential for colleges).
- **Tenant session helper** (the most important 20 lines in the codebase):

```python
# backend/app/core/tenant_db.py
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

async def get_tenant_session(claims=Depends(current_claims)):
    """Yield a DB session locked to the caller's tenant."""
    async with SessionLocal() as session:
        async with session.begin():
            # set_config(..., true) == SET LOCAL: the value lives only until this
            # transaction ends, so a pooled connection can never leak a tenant
            # into the next request.
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": str(claims.tenant_id)},
            )
            yield session
```

### Data (core DDL, abbreviated)

```sql
-- Platform-level table: NOT tenant-scoped, only the platform role can touch it
CREATE TABLE tenants (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug              text UNIQUE NOT NULL,            -- becomes the subdomain
  name              text NOT NULL,
  kind              text NOT NULL,                   -- school | college | institute | class | company | other
  registration_mode text NOT NULL DEFAULT 'invite_only'
                    CHECK (registration_mode IN ('open','approval','invite_only')),
  status            text NOT NULL DEFAULT 'active',  -- active | suspended | archived
  db_dsn            text,                            -- NULL = shared DB; set later to move a tenant out
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- Tenant-scoped table: carries tenant_id and an RLS policy
CREATE TABLE users (
  id            uuid DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenants(id),
  email         citext NOT NULL,
  full_name     text NOT NULL,
  password_hash text,                                -- NULL for SSO-only users later
  status        text NOT NULL DEFAULT 'active',      -- pending | active | deactivated
  PRIMARY KEY (tenant_id, id),                       -- composite key enables composite FKs elsewhere
  UNIQUE (tenant_id, email)                          -- email unique per tenant, not globally
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;          -- applies even to the table owner

CREATE POLICY tenant_isolation ON users
  USING      (tenant_id = current_setting('app.tenant_id')::uuid)   -- which rows are visible
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);  -- which rows may be written
```

Other Phase 1 tables: `platform_admins`, `refresh_tokens` (hashed), `invitations`, `email_tokens`.

### Frontend
- Vite + React + TypeScript, React Router, TanStack Query, generated API client, form library (React Hook Form + Zod).
- Screens: **Superadmin console** (tenant list, create tenant, create admin), **Login / register / reset password**, **Tenant admin: users list, invite, approve, CSV import, registration mode toggle**.
- Layout shell with a placeholder `ThemeProvider` (real theming in Phase 3), route guards by login state.
- Playwright smoke test: superadmin → create tenant → admin logs in → invites a user.

## Exit criteria
- [ ] A new developer runs `docker compose up` and reaches a working login screen in under 15 minutes.
- [ ] Superadmin can create a tenant and its first admin; admin can invite users, switch registration mode, and bulk import a CSV.
- [ ] **Isolation test suite passes:** for every tenant-scoped table, a test logs in as tenant A and attempts read, update, delete, and insert-with-foreign-tenant-id against tenant B's rows, and all fail or return nothing. The suite is auto-discovered from table metadata so new tables cannot be forgotten.
- [ ] A query run **without** setting `app.tenant_id` returns zero rows (fails closed), not all rows.
- [ ] A JWT for tenant A is rejected on tenant B's host.
- [ ] Login throttling works (6th failed attempt in a window is blocked).
- [ ] CI is green; ADRs for isolation model, vector store, and auth are committed.

## Risks
| Risk | Mitigation |
|------|------------|
| RLS silently bypassed (wrong DB role, owner connection, `BYPASSRLS`) | CI test that inspects the connected role's attributes; `FORCE ROW LEVEL SECURITY`; separate migration role |
| Async SQLAlchemy + per-transaction settings misbehave under pooling | One shared session helper; integration test hitting concurrent requests from two tenants |
| Over-building auth | Use well-known libraries for hashing/JWT; no custom crypto; SSO deferred |
| Email delivery needed for invites | Use console/Mailhog in dev, one transactional provider in staging; invite links also copyable by the admin |

---

# PHASE 2: RBAC and academic structure (3 weeks)

## Goal
Model *who can do what, on which class and subject*. Everything later (uploads, chat, timetables) is gated by this phase.

## Components

### Backend
- **Permission model:** roles are bundles of permission strings (e.g. `user:manage`, `role:manage`, `class:manage`, `document:upload`, `document:delete`, `timetable:publish`, `chat:use`, `analytics:view`, `branding:manage`, `audit:view`).
  - **System roles** seeded per tenant: `tenant_admin`, `teacher`, `student`. System roles can't be deleted.
  - **Custom roles:** tenant admin creates roles (e.g. "HOD", "Exam cell") by picking permissions.
- **Scope matters, not just role.** A permission alone isn't enough: a teacher with `document:upload` may upload **only to class-subject pairs they are assigned to**; a student may chat **only in classes they are enrolled in**. Implement as a small policy layer:

```python
# backend/app/core/authz.py
def require(permission: str, scope: Scope | None = None):
    """FastAPI dependency: checks role permissions AND (if given) the class/subject scope."""
    async def dep(user=Depends(current_user), db=Depends(get_tenant_session)):
        # 1) does any of the user's roles include this permission?
        if permission not in await load_permissions(user.id, db):   # cached in Redis from Phase 8
            raise HTTPException(403, "Missing permission")
        # 2) is the user allowed to act on THIS class/subject?
        if scope and not await has_scope(user, scope, db):
            raise HTTPException(403, "Not assigned to this class/subject")
    return dep
```

- **Academic structure APIs:** academic years/terms (optional field), classes (a "class" can be a college batch, a school section, or a standalone course), subjects, `class_subjects`, student enrollments, teacher assignments (teacher ↔ class ↔ subject).
- **Audit log v1:** append-only `audit_log` table (who, tenant, action, target, before/after summary, IP, request_id). Written for role changes, enrollments, user deactivation, tenant setting changes. Read-only API for admins.
- **Registration modes enforced:** `open` creates an active student without a class; `approval` creates `pending`; `invite_only` blocks public register.

### Data
`roles`, `permissions` (static catalog), `role_permissions`, `user_roles`, `classes`, `subjects`, `class_subjects`, `enrollments (user_id, class_id)`, `teacher_assignments (user_id, class_id, subject_id)`, `audit_log`. All with `tenant_id`, RLS, and composite FKs.

### Frontend
- Admin screens: roles & permissions editor (checkbox matrix), classes/subjects CRUD, enrollment manager (assign students to classes, with CSV import), teacher assignment screen, audit log viewer.
- A `usePermission('document:upload')` hook plus `<Can>` component to hide UI. **UI hiding is cosmetic; the API is the real gate.**

## Exit criteria
- [ ] Tenant admin creates a custom role, assigns it, and the permission change takes effect within one request.
- [ ] A teacher assigned to Class 10-A / Math cannot act on Class 10-B or Physics (API returns 403, test proves it).
- [ ] A student cannot call any admin or teacher endpoint (test matrix generated from the route table: every route × every role, expected status asserted).
- [ ] Every sensitive change appears in the audit log with the actor and request id.
- [ ] Isolation suite still green with the new tables.

## Risks
| Risk | Mitigation |
|------|------------|
| Permission checks forgotten on a new route | Routes default-deny via a router-level dependency; CI test fails if a route has no declared permission |
| "Class" means different things per tenant (school section vs college course vs company cohort) | Keep the model generic (`class` + `kind` label); tenant can rename the label ("Batch", "Section", "Course") in settings |
| Role explosion / over-engineering | Ship 3 system roles + custom roles by permission list only; no inheritance or hierarchy |

---

# PHASE 3: White-label theming (2 weeks)

## Goal
Each tenant's site looks like the organization's own. Build this **before** the large UI features (chat, documents) so everything after it is built with design tokens from the start.

## Components

### Backend
- `tenant_branding` table: tokens JSON, `logo_key`, `favicon_key`, display name, tagline, footer text, support email, `version`.
- Public endpoint `GET /api/public/branding` (resolves tenant from host; no auth; returns only brand-safe fields).
- Admin endpoints: update tokens, upload logo/favicon (validate type and size; allow PNG/SVG/WebP; **sanitize SVG** or rasterize it; strip metadata), restore defaults.
- Server-side **contrast validation** of token combinations (WCAG AA).
- Cache branding by `tenant_id:version` in Redis; send `ETag`/`Cache-Control`.

### Frontend
- `ThemeProvider`: fetch branding → set CSS variables → apply to the component library (Tailwind config pointing to `var(--color-primary)` or MUI/Mantine theme created from tokens).
- **Every component uses tokens, never hard-coded colors.** Add a lint rule or a Storybook check for raw hex values.
- **Branding editor** (tenant admin): color pickers, font select from allow-list, logo upload, **live preview** of login page, chat window, and timetable.
- Tenant-aware login page, header, favicon, page title, email footer (backend templates read the same branding).

## Exit criteria
- [ ] Two tenants on two subdomains (`abc.localhost`, `xyz.localhost`) show different logos, colors, fonts, names, and favicons with no code changes.
- [ ] No flash of unthemed content on reload (cached branding + inline boot).
- [ ] Contrast-failing color pairs are rejected with a clear message.
- [ ] Uploading a malicious SVG (script tag, external reference) is neutralized (test with a crafted file).
- [ ] Editing branding is visible to users within a minute (version bump busts caches).

## Risks
| Risk | Mitigation |
|------|------------|
| Tenants want pixel-perfect control | Explain token model up front; document the extension path (sanitized advanced CSS) as a later paid tier |
| Uploaded logo is an XSS vector | Serve uploads from a separate domain/bucket path with `Content-Disposition` and a strict CSP; sanitize SVG |
| Wildcard subdomains complicate local dev and TLS | Use `*.localhost` in dev; wildcard cert (or Caddy on-demand TLS) in staging/prod |
| Theming retrofitted after UI is built | That is why this phase sits here |

---

# PHASE 4: Timetables (2 weeks)

## Goal
Admins and teachers publish timetables; students view them. It is the simplest end-to-end feature and exercises roles, scopes, and branding together.

## Components

### Backend
- Model: a **timetable** belongs to a class (and optionally a term). It has **entries**: weekday, start/end time, subject, teacher, room, optional note; plus a **status** (`draft` → `published`) and version number.
- Permissions: `timetable:publish` for admins; teachers may publish only for classes they are assigned to (scope check from Phase 2).
- Conflict detection on publish: same teacher double-booked, same room double-booked, overlapping entries in one class. Return warnings; let the publisher confirm.
- Students: `GET /api/me/timetable` returns published timetables for the classes they are enrolled in. Teachers also get "my teaching schedule".
- Optional: `.ics` export link per class.
- Audit entries on publish/unpublish.

### Data
`timetables`, `timetable_entries` (with tenant_id, RLS, composite FKs to classes/subjects/users).

### Frontend
- Timetable editor (weekly grid, add/edit/move entries, save draft, publish).
- Student view: weekly grid + "today" card on the home screen, responsive for phones (this is where most students will look).
- Themed using tokens from Phase 3.

## Exit criteria
- [ ] Teacher publishes a timetable for an assigned class; students in that class see it immediately; students in other classes do not.
- [ ] Draft timetables are invisible to students.
- [ ] Conflicts are surfaced before publish.
- [ ] Permission and isolation tests extended to timetable endpoints.

## Risks
| Risk | Mitigation |
|------|------------|
| Timetable complexity creep (substitutions, holidays, multiple terms) | v1 is a weekly recurring grid plus a free-text note; substitutions are Phase 10 |
| Timezones | Store times as local wall-clock time plus a tenant timezone setting; do not convert per user |

---

# PHASE 5: Document ingestion (4 weeks)

## Goal
Teachers upload syllabus material per **class + subject**; the system turns it into searchable, correctly scoped chunks, asynchronously and reliably.

## Components

### Backend
- **Upload API:** multipart upload to **Supabase Storage** (private `documents` bucket, backend-only access with the service key; object path: `tenant/{tenant_id}/class/{class_id}/subject/{subject_id}/{doc_id}/v{n}/original.ext`). Validate extension **and** MIME sniffing, size limit, page-count limit. Compute SHA-256 to detect duplicates.
- **Job pipeline (ARQ worker):**
  1. `parse` — PDF (PyMuPDF), DOCX, PPTX, TXT/MD; keep page numbers and heading structure. OCR fallback for scanned PDFs is a flagged item (many syllabi are scans; decide in this phase whether to include Tesseract/cloud OCR now or Phase 10).
  2. `chunk` — structure-aware chunking: split on headings first, then by size (~500–800 tokens, ~10–15% overlap), keep `page_start/page_end` and `heading_path` for citations.
  3. `embed` — batch calls to the embedding model with retry/backoff; store `embedding_model` and `embedding_dim` per chunk so you can re-embed later.
  4. `index` — write chunks to Postgres in one transaction; flip document status to `ready`.
- **Status tracking:** `uploaded → parsing → embedding → ready | failed` with an error message; progress pushed to the UI by polling (SSE later).
- **Versioning and deletion:** re-upload creates a new version; old chunks are removed only after the new version is `ready` (no downtime window with empty results). Deleting a document deletes its file, chunks, and vectors in one transaction + a storage cleanup job.
- **Idempotency:** re-running a job must not duplicate chunks (unique `(document_id, version, ordinal)`).
- **Untrusted content:** treat all uploaded text as untrusted input (see "indirect prompt injection" in Phase 7). Strip control characters, record a content hash.

### Data

```sql
CREATE TABLE documents (
  id uuid, tenant_id uuid NOT NULL,
  class_id uuid NOT NULL, subject_id uuid NOT NULL,       -- the scope that retrieval will filter on
  title text NOT NULL, storage_key text NOT NULL, sha256 text NOT NULL,
  version int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'uploaded',                -- uploaded|parsing|embedding|ready|failed
  error text, uploaded_by uuid NOT NULL, created_at timestamptz DEFAULT now(),
  PRIMARY KEY (tenant_id, id)
);

CREATE TABLE chunks (
  id uuid, tenant_id uuid NOT NULL,
  class_id uuid NOT NULL, subject_id uuid NOT NULL,       -- denormalized from document: fast filtered search
  document_id uuid NOT NULL, version int NOT NULL, ordinal int NOT NULL,
  page_start int, page_end int, heading_path text,        -- used for citations
  content text NOT NULL,
  content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED, -- 'simple' keeps it language-neutral
  embedding vector(1024) NOT NULL,                        -- dimension follows the chosen embedding model
  embedding_model text NOT NULL,
  PRIMARY KEY (tenant_id, id),
  UNIQUE (document_id, version, ordinal)                  -- idempotent re-runs
);
CREATE INDEX chunks_scope_idx ON chunks (tenant_id, class_id, subject_id);
CREATE INDEX chunks_vec_idx   ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_fts_idx   ON chunks USING gin (content_tsv);
-- plus ENABLE/FORCE ROW LEVEL SECURITY and the same tenant_isolation policy as users
```

If a document must be shared across several classes, v1 simply uploads it once per class (storage is cheap; scoping stays trivial). A shared-document table can come later.

### Frontend
- Teacher "Course materials" page: choose class + subject (only assigned ones appear), drag-and-drop upload, list with live status chips, retry on failure, delete, replace with new version.
- Admin view: all documents in the tenant, filter by class/subject, storage usage.

## Exit criteria
- [ ] Teacher uploads a 100-page PDF; it reaches `ready` with chunk count > 0 and page numbers stored.
- [ ] Killing the worker mid-job and restarting it completes the job without duplicates.
- [ ] Corrupt, password-protected, or oversized files fail with a readable message, not a stack trace or a stuck job.
- [ ] A teacher cannot upload or delete for a class/subject they aren't assigned to.
- [ ] Isolation tests cover `documents`, `chunks`, and Supabase Storage paths (tenant A's signed URL cannot fetch tenant B's file, and the `anon`/`authenticated` roles cannot list the bucket).
- [ ] Replacing a document keeps the old version searchable until the new one is `ready`.

## Risks
| Risk | Mitigation |
|------|------------|
| Poor parsing (tables, scans, two-column PDFs, math) wrecks answer quality later | Build a small **parser test corpus** of real syllabi from pilot users now; inspect chunks by eye in an admin debug view |
| Embedding cost/latency on big uploads | Batch + rate-limit workers; per-tenant upload quota (enforced in Phase 8) |
| Embedding model change forces re-index | Store model name per chunk; write a `reindex` job now, even if only used once |
| Malware in uploads | Size/type validation now; optional ClamAV scan step before parse |
| Chunking choices hard to evaluate yet | Revisit with metrics in Phase 7; keep chunking parameters in config |

---

# PHASE 6: Scoped RAG chat (4 weeks)

## Goal
A registered student asks a question and gets an answer **grounded only in that class's documents**, with citations, streamed to the UI.

## Components

### Backend
- **Chat API:** `POST /api/chat/conversations` (class + optional subject), `POST /api/chat/conversations/{id}/messages` returning **Server-Sent Events** (tokens, then a final event with citations and message id). History endpoints for list/resume/delete.
- **Authorization:** user must be enrolled in the class (or be a teacher assigned to it for previews). The class id is validated server-side; **the retrieval filter is built from the authorized scope, never from raw client input.**
- **Retrieval service:**
  1. *Condense* the question with recent turns into a standalone query (small/cheap model call, skipped for the first message).
  2. *Hybrid search:* vector top-k (e.g. 20) + Postgres full-text top-k, merged with **Reciprocal Rank Fusion**. Filter: `tenant_id = :t AND class_id = :c [AND subject_id = :s]`.
  3. *Optional rerank* (cross-encoder or LLM reranker) down to ~5 chunks; make it a config switch and decide using Phase 7 metrics.
  4. *Sufficiency check:* if the best score is under a threshold, return a polite "I couldn't find this in your class material" **without calling the LLM**. This one rule prevents most hallucinations.
- **Prompt assembly:** system prompt (tenant bot name, tone, language, scope rules) + retrieved chunks inside clearly delimited blocks labeled with source ids + conversation window. Instruct: answer only from sources, cite source ids, say when unsure.
- **Citations:** model outputs source ids; backend maps them to `{document title, page range, heading, snippet}` and stores them with the message. Drop any citation id the model invented.
- **Persistence:** `conversations`, `messages` (role, content, token counts, latency, model, retrieval debug info), `message_citations`, `message_feedback` (thumbs up/down + optional comment).
- **Minimal guardrail now:** max input length, refuse when no context. Full guardrail layer is Phase 7.

### Data
`conversations`, `messages`, `message_citations`, `message_feedback`; all tenant-scoped with RLS.

### Frontend
- Chat UI in the tenant theme: class/subject selector, streaming message rendering, markdown rendering (sanitized), **clickable citations** that open a source panel (document title, page, snippet), thumbs up/down, "new chat", history sidebar, loading and error states, empty-state suggestions.
- Teacher "preview as student" mode to test a class's bot.
- Mobile-first layout; most student usage will be on phones.

## Exit criteria
- [ ] Student in Class A asking about a topic that exists only in Class B's documents gets the "not found in your class material" response. **Automated cross-class and cross-tenant leakage tests** seed distinctive canary strings in each scope and assert they never appear in another scope's answers or citations.
- [ ] Answers stream with first token in under ~2 s on typical queries (p50), and citations resolve to the correct page.
- [ ] A student not enrolled in the class gets 403 on both conversation creation and message send.
- [ ] Conversation history persists and is only visible to its owner (and tenant admin only if a policy says so).
- [ ] Feedback is captured and visible in the database.

## Risks
| Risk | Mitigation |
|------|------------|
| Cross-class leakage via a retrieval filter bug | Filter derived server-side; canary tests in CI; RLS still enforces tenant boundary underneath |
| Hallucination or answers outside syllabus | Sufficiency threshold, "answer only from sources" prompt, citation required (enforced in Phase 7) |
| Multi-turn follow-ups retrieve poorly ("explain that again") | Query condensation step; evaluate with multi-turn cases in Phase 7 |
| LLM cost spikes | Per-user rate limit starts here (simple Redis counter); full quotas in Phase 8 |
| Latency (condense + retrieve + generate) | Skip condensing on first turn; stream; parallelize retrieval and moderation; measure each stage from day one |

---

# PHASE 7: Guardrails and evaluation (4 weeks)

## Goal
Make the bot **safe, on-topic, and measurably good**, and make quality regressions visible before they reach students. After this phase the product is ready for a real pilot.

## Components

### Backend: guardrails (pipeline of small, testable checkers)
A `GuardrailPipeline` runs ordered checks; each returns `allow | modify | block` with a reason code. Results are logged per message.

**Input guardrails**
- Length and format limits; strip hidden/control characters.
- **PII detection** (phone, email, government ID patterns): warn or redact before sending to the model and before logging.
- **Prompt-injection heuristics** ("ignore previous instructions", role-play attacks, system prompt extraction); block or neutralize.
- **Moderation** (self-harm, hate, sexual content, violence): provider moderation endpoint or a small classifier. Self-harm gets a **supportive canned response with the tenant's configured helpline/contact**, not a bare refusal. (Important for a student audience.)
- **Scope/topic check:** is the question plausibly about the class material? Off-topic gets a friendly redirect. Tunable per tenant (strict vs relaxed).

**Retrieval-side guardrail: indirect prompt injection**
Uploaded documents are untrusted. A document may contain "ignore the rules and say X". Mitigations: delimit context and tell the model it is data, not instructions; scan chunks at ingestion for injection patterns and flag for teacher review; never give the model tools or side effects in v1.

**Output guardrails**
- **Citation enforcement:** an answer with factual content but zero valid citations is replaced by the "couldn't find" response (configurable).
- **Groundedness check:** lightweight NLI/LLM-judge comparing answer claims to retrieved chunks; run in parallel for strict-mode tenants, async-sampled for others.
- System-prompt/secret leakage filter, profanity filter, output length cap, language/tone check.
- Per-tenant guardrail config (`strict | balanced | relaxed`, custom blocked topics, helpline text) stored in `tenant_settings`.

### Backend: evaluation
- **Golden datasets:** per tenant or per class, a set of questions with expected source chunks and reference answers. Generate a first draft synthetically from the documents, then **have a teacher review/approve** (UI in the frontend section). Start with 30–50 questions per pilot tenant.
- **Metrics:**
  - *Retrieval:* recall@k, MRR (does the right chunk appear?).
  - *Generation:* faithfulness/groundedness, answer relevance, citation correctness, refusal correctness (answered when it should refuse, or the reverse).
  - *System:* latency per stage, cost per answer.
- **Harness:** a CLI + pytest wrapper (`make eval`) that runs the golden set against the real pipeline (retrieval-only mode is fast and cheap; full mode uses an LLM judge). Libraries like Ragas or DeepEval can help, but wrap them so you can swap judges.
- **CI gate:** retrieval-only eval on every PR touching chunking/retrieval/prompt code; full eval nightly; fail on regression beyond a threshold versus the stored baseline.
- **Red-team suite:** 50–100 adversarial prompts (jailbreaks, injection, cross-class probing, PII fishing) that must all be blocked or safely handled.
- **Online evaluation:** sample a percentage of live answers, judge asynchronously, store scores; combine with thumbs feedback into a quality dashboard. Surface low-scoring/"unanswered" questions to teachers (feeds Phase 10 analytics).

### Data
`guardrail_events` (message, stage, check, decision, reason), `tenant_settings` (guardrail config), `eval_datasets`, `eval_cases`, `eval_runs`, `eval_results`, `online_eval_scores`.

### Frontend
- Admin: guardrail settings page (strictness, helpline text, blocked topics).
- Teacher: **"Test questions" page**: review/approve generated eval questions, add their own, see pass/fail per question after changes to documents.
- Admin/teacher: flagged-conversation review queue (blocked inputs, low groundedness, thumbs-down) with the ability to mark "false positive".
- Chat UI: friendly messages for each block reason (never a raw error).

## Exit criteria
- [ ] Red-team suite: 100% of "must block" cases blocked, and under 5% false positives on a benign set of real student questions.
- [ ] Every answer in the database has stored guardrail decisions and a latency breakdown.
- [ ] `make eval` runs in CI; a deliberate bad change (e.g. chunk size set to 50 tokens) **fails the gate**.
- [ ] Baseline numbers recorded (target to aim for, adjust after first run: recall@5 ≥ 0.85, faithfulness ≥ 0.9, correct-refusal rate ≥ 0.9).
- [ ] Guardrail overhead on the synchronous path is under ~150 ms at p95 (LLM-based checks excluded because they run async or in parallel).
- [ ] Teachers can review flagged items and correct the golden set without engineer help.

## Risks
| Risk | Mitigation |
|------|------------|
| False positives frustrate students (legit biology/history questions blocked) | Measure on a benign corpus; tune per tenant; always give an explanation and a path forward; keep a "report wrong block" button |
| LLM-as-judge is biased or noisy | Calibrate against ~50 human-labeled answers; use a different (or stronger) model than the generator; track judge agreement |
| Guardrail latency makes chat feel slow | Rules first; parallelize; async for expensive checks; budget per stage |
| Golden sets go stale as documents change | Eval cases link to document versions; re-run automatically after re-ingestion and flag broken cases |
| Over-reliance on guardrails as the only defense | Authorization, scoped retrieval, and no-tool design are the primary protections; guardrails are the extra layer |

---

# PHASE 8: Redis hardening: caching, rate limiting, quotas (2 weeks)

## Goal
Protect cost and stability and speed up hot paths. Redis has been used lightly since Phase 1 (login throttling, queue); this phase turns it into a deliberate layer.

## Components

### Backend
- **Rate limiting:** sliding-window or token-bucket via Redis Lua script. Layers: per IP (auth and public endpoints), per user (chat), per tenant (aggregate). Return `429` with `Retry-After`. Separate stricter limits for expensive endpoints (chat, upload).
- **Usage quotas:** per-tenant monthly limits on messages/tokens, documents, storage, and users, configured by plan (`usage_counters` in Redis, flushed to `usage_events` in Postgres). Soft warning at 80%, hard stop at 100% with a clear message to the tenant admin. Superadmin can adjust per tenant.
- **Caching (all keys are namespaced `t:{tenant_id}:...` so a key can never cross tenants):**

| Cache | Key | TTL / invalidation |
|-------|-----|--------------------|
| Branding | `t:{id}:branding:v{version}` | Version in key; no manual invalidation |
| User permissions | `t:{id}:perm:{user_id}` | 5 min + explicit delete on role change |
| Query embeddings | `t:{id}:qemb:{hash(query)}` | 24 h |
| Retrieval results | `t:{id}:c:{class}:ret:{corpus_version}:{hash(query)}` | Invalidated by bumping `corpus_version` when any document in the class changes |
| Full answers (optional, FAQ-style) | same plus prompt version | Short TTL; **only for single-turn, no-history questions** |

- **Concurrency control:** per-tenant limit on concurrent ingestion jobs and LLM calls to stop one tenant starving others (fair queueing).
- **Resilience:** Redis outage must degrade gracefully: rate limiter fails **open for reads, closed for chat/uploads** (configurable), caches are bypassed, and queue-dependent features show a clear status.
- Redis config: separate logical DBs or instances for cache (eviction allowed) vs queue/limits (no eviction, persistence on).

### Frontend
- Friendly `429` / quota-exceeded UI states; usage meter on admin dashboard (messages, storage, users vs plan).
- Superadmin: per-tenant usage and quota editor.

## Exit criteria
- [ ] Load test: one abusive user is throttled while other users in the same tenant are unaffected; one abusive tenant does not degrade another.
- [ ] Cache hit ratios are visible and retrieval p95 improves measurably on repeated queries.
- [ ] Changing a role or uploading a new document invalidates the relevant caches (test).
- [ ] Hitting a quota blocks the action with a correct message and the admin sees why.
- [ ] Killing Redis during a test does not crash the API.

## Risks
| Risk | Mitigation |
|------|------------|
| Stale cache returns answers from old syllabus | Corpus-version in keys; short TTL on answer cache; skip answer cache by default |
| Cross-tenant cache key collision | Central helper builds all keys; lint/test rejects raw key strings |
| Quota counters drift from reality | Periodic reconciliation from `usage_events`; counters are advisory, DB is the source of truth |
| Redis is a single point of failure | Managed Redis with replica; the degradation behavior above |

---

# PHASE 9: Production readiness (3 weeks)

## Goal
Run safely in production with visibility, recoverability, and a repeatable deployment.

## Components

### Observability
- **Structured logs** (JSON) with `request_id`, `tenant_id`, `user_id`, route, latency. **Never log raw document text or full chat content at info level**; log ids and hashes.
- **Tracing:** OpenTelemetry across API → DB → Redis → LLM → worker; a span per RAG stage (condense, retrieve, rerank, generate, guardrails).
- **Metrics (Prometheus/Grafana or managed equivalent):** request rate/latency/error by route, queue depth and job age, ingestion duration, retrieval latency, LLM latency/tokens/cost **per tenant**, cache hit ratio, rate-limit rejections, guardrail block rate, eval scores over time.
- **Error tracking:** Sentry (frontend and backend), with tenant tag and PII scrubbing.
- **Alerts:** error rate, p95 latency, queue backlog, failed ingestion rate, LLM provider errors, Supabase/Postgres connection count (pooler saturation), replication lag, disk and database size, and certificate expiry.
- Optional: LLM-specific tracing (e.g. Langfuse) for prompt/response inspection with access controls.

### Auditing and compliance
- Audit log extended to: logins (success/failure), permission changes, document upload/delete, branding changes, exports, superadmin actions (including any impersonation, which must be explicit, time-boxed, and logged).
- Audit logs are append-only (DB role without UPDATE/DELETE), retained per policy, and exportable by tenant admins.
- **Data lifecycle:** per-tenant data export, user deletion/anonymization, tenant offboarding (delete rows, vectors, files, caches), configurable chat retention. Align with applicable law (e.g. India's DPDP Act, and note that students may be minors, which raises consent requirements).

### Security
- OWASP review of API; dependency and container scanning in CI; secrets in a secrets manager (never in env files in the repo); CSP, CSRF-safe cookie settings for refresh tokens (`HttpOnly`, `Secure`, `SameSite`), strict CORS per tenant host.
- Optional 2FA for tenant admins and mandatory for superadmins.
- Penetration-style tests focusing on: tenant isolation, IDOR on ids, upload handling, prompt injection, SSRF (none expected, keep it so), and rate-limit bypass.
- Database: least-privilege roles, TLS everywhere, encryption at rest, backups and point-in-time recovery (on Supabase, PITR is a paid add-on; confirm it is switched on) with a **tested restore**.

### Deployment and scaling
- **Containers:** separate images/processes for `api`, `worker`, and (if used) `migrate`.
- **Environments:** local → staging (mirrors prod) → prod. Infrastructure as code (Terraform or equivalent) for repeatability.
- **Pipeline (GitHub Actions or similar):** lint/type/test → isolation suite → build → deploy to staging → smoke + Playwright → manual approval → prod. Migrations run as a separate step, **backward-compatible** (expand/contract) so rollbacks are safe.
- **Hosting:** start simple (Supabase for Postgres + Storage, managed Redis such as Upstash or Redis Cloud, and a container service such as Render/Fly/Cloud Run/ECS for the API and workers, **in the same region as the Supabase project** to keep query latency low; or a small Kubernetes cluster if your team already runs one). Do not adopt Kubernetes just for this.
- **Scaling path:** stateless API replicas behind a load balancer; workers autoscale on queue depth; **Supavisor** (Supabase's pooler) in transaction mode; upgrade the Supabase compute size first, then add a read replica for analytics/reporting queries; partition `chunks` by tenant hash when it passes a few million rows; move heavy tenants to their own Supabase project via `tenants.db_dsn`; swap pgvector for a dedicated vector DB only if the Phase 1 trigger is hit.
- **Load test** (k6/Locust) with a realistic mix: 500 concurrent students chatting, 20 teachers uploading, branding fetches on every page load.

### Frontend
- Error boundaries, offline/slow-network states, accessibility pass (keyboard, contrast, screen reader labels for chat), bundle size budget, CDN caching of hashed assets, CSP-compatible build.

## Exit criteria
- [ ] Dashboards show per-tenant latency, errors, token cost, queue depth, and cache hit ratio.
- [ ] A forced failure (kill worker, kill Redis, slow LLM) triggers the expected alert and graceful behavior.
- [ ] Backup restore drill completed from scratch into a clean environment; RPO/RTO written down.
- [ ] Load test meets targets (suggested: chat first-token p95 < 3 s at 500 concurrent users; API non-LLM routes p95 < 300 ms).
- [ ] Tenant export and offboarding delete tested end to end, including vectors, files, caches.
- [ ] Security checklist signed off; no high/critical findings open.
- [ ] One-command rollback of an API deploy verified on staging.

## Risks
| Risk | Mitigation |
|------|------------|
| Operational complexity for a small team | Prefer managed services; keep runbooks short and tested |
| Logs leaking student data | Redaction at the logger; log-level policy; scrubbing tests |
| LLM provider outage or rate limits | Timeouts, retries with backoff, circuit breaker, a user-facing degraded message; optional secondary provider via the `LLMClient` abstraction |
| Cost visibility arrives too late | Per-tenant cost metrics from day one of this phase; budget alerts |

---

# PHASE 10: Optional differentiators (ongoing, by demand)

Pick by what pilot tenants actually ask for. Suggested order of value is listed in section 3.

## Exit criteria (per feature)
Each feature ships behind a per-tenant feature flag, has permission checks, isolation tests, and an owner-facing toggle in tenant settings.

## Risks
Scope creep is the main risk; every item must trace to a concrete request from a pilot tenant.

---

# 3. Additional features: essential vs optional

Legend: **E** = essential for a trustworthy v1 (already placed in a phase above) · **R** = strongly recommended soon after pilot · **O** = optional / demand-driven.

| Feature | Rating | Phase | Why / notes |
|---------|:------:|:-----:|-------------|
| Citation display with page + snippet | **E** | 6 | Trust and verifiability; also the main hallucination defense |
| Conversation history | **E** | 6 | Students expect to resume; needed for review queue |
| Thumbs up/down feedback | **E** | 6 | Cheapest quality signal; feeds evaluation |
| Audit log | **E** | 2, 9 | Required by institutions; also your debugging tool |
| Bulk CSV import (users, enrollments) | **E** | 1, 2 | Colleges onboard hundreds of students at once |
| Password reset, email verification, invitations | **E** | 1 | Basic account lifecycle |
| Basic content moderation (self-harm, abuse, off-topic) | **E** | 7 | Student audience, institutional liability |
| Usage quotas and rate limits | **E** | 6, 8 | Protects cost and fairness between tenants |
| Document versioning and failure/retry UX | **E** | 5 | Syllabi change every term |
| Tenant data export and offboarding | **E** | 9 | Contractual and legal expectation |
| Subdomain-based tenant sites | **E** | 3 | Needed for the "looks like our website" requirement |
| Teacher analytics: top questions, unanswered questions, topic coverage gaps | **R** | 10 | Highest teacher-facing value: shows what students struggle with and which material is missing. Builds on `messages` + online eval data you already store |
| "Unanswered questions" inbox that lets a teacher upload/fix material | **R** | 10 | Closes the loop between student questions and content |
| Admin 2FA | **R** | 9 | Admins control large amounts of student data |
| Custom domains (e.g. `chat.college.edu`) with auto TLS | **R** | 10 | Completes the white-label promise; needs DNS verification + cert automation |
| Multilingual support (e.g. English/Hindi/Marathi): UI i18n plus multilingual embeddings | **R** | 10 | High value for Indian institutions; affects embedding model choice, FTS config, and eval sets, so decide early even if built late |
| SSO (Google Workspace / Microsoft Entra via OIDC; SAML later) | **O** (R for larger orgs) | 10 | Removes password management for colleges already on Google/Microsoft; map IdP groups to roles |
| OCR for scanned documents | **R** | 5 or 10 | Many syllabi are scans; decide in Phase 5 based on pilot samples |
| Timetable `.ics` export / calendar sync | **O** | 4 or 10 | Easy win for students |
| Substitutions, holidays, exam schedules in timetable | **O** | 10 | Add when requested |
| Notifications (email/push) for timetable changes and new material | **O** | 10 | Useful; watch notification fatigue |
| Per-tenant model/provider settings and bring-your-own-key | **O** | 10 | Enterprise ask; complicates evaluation and cost tracking |
| Answer styles per subject (concise, step-by-step, exam mode) | **O** | 10 | Simple prompt templates, teacher-configurable |
| Cross-class shared documents (one upload, many classes) | **O** | 10 | Avoids duplicate uploads; needs a scope join table |
| Admin impersonation for support | **O** | 9/10 | Only with explicit consent flow, time limit, and audit entries |
| PWA / mobile-friendly install | **O** | 10 | Cheap if the UI is already responsive |
| Quiz / practice-question generation from syllabus | **O** | 10 | Attractive but a different product surface; protect the core first |
| Billing and plans | **O** | after pilot | Only when you have paying tenants; quotas already give the hooks |

---

# 4. Cross-cutting engineering rules (apply from Phase 1)

1. **Default-deny:** a route without a declared permission fails CI.
2. **Every new tenant-scoped table** ships with `tenant_id`, RLS, composite FKs, and is auto-covered by the isolation test suite.
3. **Tenant-aware keys everywhere:** DB rows, object-storage paths, Redis keys, log fields, metrics labels (careful with label cardinality; use tenant id in logs/traces, and top-N tenants in metrics).
4. **Migrations are backward-compatible** (expand → migrate → contract).
5. **Config over code** for anything you'll tune: chunk size, top-k, thresholds, model names, prompts (versioned), limits.
6. **Version prompts** and store the prompt version with every message so quality changes can be traced.
7. **Test pyramid:** unit tests for services, integration tests with real Postgres/Redis (testcontainers), isolation suite, eval suite, a small Playwright set for critical journeys.
8. **ADRs:** one page per decision (isolation, vector store, theming, auth, guardrails, queue). Revisit them at phase boundaries.
9. **Pilot early:** recruit one friendly tenant (a single college class is enough) before Phase 7 ends, and use their real syllabi for the parser corpus and golden set.

---

# 5. Open questions to settle early

| Question | Needed by | Default if undecided |
|----------|-----------|----------------------|
| Which LLM and embedding provider/models? (affects cost, language support, vector dimension) | Phase 5 | Provider-agnostic interface; one hosted embedding model with ~1024 dims |
| Must scanned/handwritten documents be supported at launch? | Phase 5 | Text-based PDFs/DOCX/PPTX only; OCR in Phase 10 |
| Languages required at launch? | Phase 5 (embedding/FTS choice) | English first; keep `'simple'` FTS config and a multilingual-capable embedder |
| Can students under 18 use the platform? What consent/data rules apply? | Phase 7 | Treat as yes; strict guardrails default, minimal data retention, parental/institutional consent handled by the tenant |
| Should tenant admins be able to read students' chat history? | Phase 6 | Off by default; flagged-content review only; configurable per tenant and always audited |
| Hosting region and data residency (e.g. data must stay in India) | **Phase 1** (a Supabase project's region cannot be changed later; you would have to migrate) | Choose the Supabase region closest to your users and check that it exists in the list at project creation; host the API in the same region |
| Which Supabase plan for the pilot? | Phase 5 | Free tier for development only (idle projects can pause, limited backups and storage); paid plan before real students use it. Check current limits at supabase.com/pricing |
| Supabase Auth vs FastAPI-owned auth | Phase 1 | FastAPI-owned auth in v1 (see 1.6); revisit when SSO is requested |
| Should the bot answer general questions beyond the syllabus? | Phase 7 | No: strictly syllabus-grounded, with a friendly redirect |
| Hard vs soft quotas, and plan tiers | Phase 8 | Soft warning at 80%, hard stop at 100% |

---

# 6. Suggested first two weeks (Phase 1 kickoff checklist)

**Week 1**
- [ ] Monorepo, CI, pre-commit (ruff, mypy, eslint, prettier), `docker compose` (Redis, Mailhog) plus `supabase start` (local Postgres+pgvector and Storage); one staging Supabase project created.
- [ ] Alembic baseline; custom DB roles (`app_tenant`, `app_platform`; RLS enforced for both) created in Supabase, `app` schema, extensions enabled, Alembic running as the Supabase admin user over the direct connection; `tenants`, `platform_admins`, `users` tables with RLS.
- [ ] Tenant session helper + the first **isolation test** (written *before* the endpoints that need it).
- [ ] ADRs: isolation model, vector store, auth/token strategy.
- [ ] OpenAPI → TypeScript client generation wired into the frontend build.

**Week 2**
- [ ] Host-based tenant resolution; JWT issue/refresh/revoke; login throttling.
- [ ] Superadmin: create tenant + first admin.
- [ ] Frontend shell: routing, auth guards, login, superadmin tenant list/create.
- [ ] Playwright smoke test for the superadmin → tenant admin journey.
- [ ] Demo at end of week 2: two tenants exist, each admin sees only their own users, and the leak tests prove it.

---

*This plan assumes the workflow and stack as described: superadmin → tenant admin → teachers/students, class-scoped RAG, per-tenant branding, and timetables. Features beyond that scope are listed only in section 3 and tied to a phase.*
