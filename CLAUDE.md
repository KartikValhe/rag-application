# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Multi-tenant white-label academic platform (named "RAG" but no RAG/vector code exists yet; see `docs/adr/002-vector-store.md`). Backend: FastAPI + async SQLAlchemy + Postgres 16 RLS + Redis. Frontend: React 18 + Vite + TS + Tailwind. `README.md` has demo accounts, ports and deployment notes.

## Project rules (read first)

- Source of truth: `docs/implementation_plan.md`. Follow it; if you disagree, say so before changing course.
- Stack: FastAPI + SQLAlchemy async + Alembic + PostgreSQL (pgvector, RLS), Redis, React + TS + Tailwind.
- Backend only unless told otherwise. Do NOT edit `frontend/` (another agent owns it).
- Every tenant table: `tenant_id`, RLS ENABLE+FORCE, composite FKs, covered by the isolation tests.
- Default-deny permissions; same generic 404 for "not found" and "not yours".
- Never read or print `.env` files, secrets or real credentials.
- Work in small steps: plan first, then code, then tests. Run `pytest -q`, `ruff check .`, `mypy app` before reporting.
- Every fix gets a regression test. Do not weaken or skip tests.
- Keep code comments plain English. Keep output short: summaries, not full file dumps.

## Commands

Infra (Postgres on **5433**, Redis 6379, Mailpit 1025/8025): `docker compose up -d`

Backend (from `backend/`; dependencies are in `pyproject.toml` — there is no requirements.txt despite what the README says):
```bash
pip install ".[dev]"
alembic upgrade head
python -m app.scripts.seed                 # demo tenants/users (acme, default) + superadmin
uvicorn app.main:app --reload --port 8000
ruff check .            # lint (CI)
mypy app                # strict mode (CI)
pip-audit               # CI
pytest                  # needs running Postgres + Redis (migrated DB)
pytest tests/test_isolation.py -k <name> -v   # single test/file
```
Tests are async (`asyncio_mode=auto`, session-scoped loop). `tests/conftest.py` defaults to the docker-compose DB/Redis (Redis DB 1) and uses the superuser `MIGRATION_DATABASE_URL` for fixtures to bypass RLS.

Frontend (from `frontend/`):
```bash
npm run dev | build | typecheck
npm run lint            # eslint + check:styles
npm test                # node --test on an explicit file list in package.json — add new test files there
node --test --experimental-strip-types path/to/file.test.ts   # single test
npm run test:e2e        # Playwright; needs backend + frontend running with seeded data
```

## Architecture

**Three DB roles / sessions** (`backend/app/core/tenant_db.py`, `infra/postgres/init.sql`, ADR 001): `app_tenant` (NOBYPASSRLS) for tenant routes, `app_platform` for platform tables only (`tenants`, `platform_admins`), and a superuser used for migrations (`MIGRATION_DATABASE_URL`). `get_tenant_session` opens a transaction and runs `set_config('app.tenant_id', …, true)` (transaction-local); every tenant table has `tenant_id` + FORCE RLS, composite `(tenant_id, id)` keys. Queries without tenant context return zero rows. New tenant tables need an RLS policy in their Alembic migration and coverage in `tests/test_isolation.py`.

**Tenant resolution**: from the `Host` header subdomain (or `?tenant=` in dev) via middleware in `app/core/middleware.py` / `app/core/context.py`; JWT auth then populates the request tenant. Reverse proxies must preserve `Host`.

**Authorization** (`app/core/authz.py`, `app/services/rbac.py`): default-deny — every route must use `require(permission, scope)`. Scoped checks (teacher → assigned classes/subjects, student → enrolled classes) return a generic 404 rather than 403 to prevent ID probing. System roles (`tenant_admin`, `teacher`, `student`) are immutable and admins can't grant permissions they lack. Mutations are written to an append-only audit log (`services/audit.py`) with secrets scrubbed.

**Auth**: Argon2id passwords, JWT with separate secrets for tenant vs platform (`SECRET_KEY` ≠ `PLATFORM_SECRET_KEY`, ≥32 chars outside development), refresh-token rotation with theft detection, Redis-backed revocation and sliding-window throttles (`app/core/redis_client.py`).

**Backend layout**: `api/v1/*` routers (aggregated in `router.py`) → `services/*` business logic → `db/models/*`. Config in `core/config.py` (pydantic-settings, see `.env.example`).

**Timetable import**: uploads are validated by `core/upload_security.py`, then parsed (xlsx/docx/pdf) in a killable `multiprocessing` child (spawn start method) with a 10s timeout, global/per-tenant concurrency caps and rate limits (`services/timetable_parser_runner.py` → `timetable_importer.py`). Keep parsing inside this runner.

**Frontend**: feature folders in `frontend/src/features/*` (auth, admin, teacher, student, timetable, branding, platform, …), TanStack Query for data, axios client in `src/api`. White-label theming is driven by per-tenant branding tokens → CSS variables (`src/styles/theme.css`, `features/branding/utils/tokens.ts`).

## Frontend style guardrails (enforced by `npm run check:styles`, part of lint/CI)

- No inline `style=` props and no direct `element.style` mutation (use the `.body-scroll-lock` class / `useScrollLock`).
- No hardcoded hex colors in components; only allowed in `theme.css`, `branding/utils/tokens.ts`, `branding/types.ts`. Use semantic Tailwind tokens.
- Mobile-first; 44px touch targets on coarse pointers; tables render as stacked cards on mobile (`Table.tsx`).

## CI

`.github/workflows/ci.yml`: backend (pip-audit, ruff, mypy, alembic, pytest on Python 3.12) and frontend (lint, unit tests, typecheck, build on Node 22).
