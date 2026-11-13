# Multi-Tenant Academic RAG Platform

A secure, multi-tenant academic platform built for universities and educational institutions. Features Row-Level Security (RLS) data isolation, granular Role-Based Access Control (RBAC), and scoped permissions for tenant administrators, faculty, and students.

---

## 🏛 System Architecture & Stack

- **Backend:** FastAPI (Python 3.12), SQLAlchemy 2.0 (async), Alembic, Pydantic v2, Argon2id, Structlog.
- **Database & Isolation:** PostgreSQL 16 with native Row-Level Security (`app.current_tenant_id`) and separate platform metadata schema.
- **Cache & Rate Limiting:** Redis 7 (sliding-window throttles, token revocation).
- **Frontend:** React 18, Vite, TypeScript, TanStack Query, React Router v6, Tailwind CSS with semantic CSS variables (`theme.css`).
- **Email Testing:** Mailpit (SMTP server & web UI).

---

## 📋 Project Status

| Phase | Scope | Status |
|-------|-------|--------|
| 1-3 | Tenancy, RLS, auth, RBAC, academic structure, branding | Done |
| 4 | Timetables, import wizard, schedule lookup, Next lecture card | **Backend done; wrap-up open** (see exit criteria below) |
| 5+ | Document ingestion, vector store, chat (RAG) | Not started |

Phase 4 exit criteria (checked against `docs/implementation_plan.md`):

| Criterion | Result | Evidence |
|-----------|--------|----------|
| Teacher publishes for an assigned class; students in it see it; other classes do not | FAIL | UI e2e `teacher drafts and publishes...` fails: teacher calls to `GET /classes/{id}/subjects` and `/timetables` return 500 on the dev backend. API tests: `test_teacher_scoping_assigned_vs_unassigned`, `test_conflict_detection_requires_explicit_confirm`. No dedicated "student in another class" test found |
| Drafts invisible to students | PASS | `test_schedule.py::test_draft_never_visible_to_students` |
| Conflicts surfaced before publish | PASS | `test_timetable_endpoints.py::test_conflict_detection_requires_explicit_confirm`, `test_schedule.py::test_conflict_detection_scenarios` |
| Permission and isolation tests cover timetable endpoints | PASS | `test_isolation.py::test_timetables_and_entries_isolation`, `test_timetable_route_role_matrix`, `test_authorization_matrix_for_import_endpoints` |
| Schedule lookup scenarios with a fixed clock | PASS | `test_schedule.py` (in-progress, later today, tomorrow, next week, weekend, none left, none published, several classes, teacher, admin, timezone) |
| Student never gets another class, a draft or another tenant | PASS | `test_cross_tenant_isolation`, `test_draft_never_visible_to_students` |
| Lookup makes zero LLM or network calls | PASS | `test_lookup_service_makes_no_network_or_llm_call` |
| Import produces drafts with per-row errors; hostile files rejected; never auto-published | PASS (backend) | `test_timetable_import.py` (renamed executable, macro workbook, zip bomb, formulas), `test_upload_hardening.py::test_oversized_content_length_rejected_413`, `test_save_imported_draft_revalidation_and_confirmation`; e2e `import wizard` tests |

Backend tests were not re-run for this table; the evidence column names the tests that cover each item.

---

## 🚀 Getting Started

### 1. Prerequisites
- Docker & Docker Compose
- Python 3.12+
- Node.js 18+ or 20+

### 2. Infrastructure Startup
Start PostgreSQL, Redis, and Mailpit using Docker Compose:
```bash
docker compose up -d
```

### 3. Backend Setup
Activate the virtual environment and install dependencies:
```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

Run database migrations:
```bash
alembic upgrade head
```

Seed initial tenants, system roles, permissions, and demo users:
```bash
python -m app.scripts.seed
```

Start the FastAPI application:
```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Frontend Setup
In a separate terminal:
```bash
cd frontend
npm install
npm run dev
```

### 5. Multi-Tenant Subdomain Testing (White-Label Theming)
All modern browsers treat `*.localhost` as loopback addresses without requiring `/etc/hosts` changes:
- **Tenant Acme:** Visit `http://acme.localhost:5173/login` &rarr; Automatically resolves `acme` tenant, displays custom Acme Academy colors, typography, logos, and copy.
- **Default Tenant:** Visit `http://localhost:5173/login` &rarr; Uses standard platform theme tokens.
- **Parameter Override:** Visit `http://localhost:5173/login?tenant=acme` &rarr; Explicitly selects the `acme` tenant branding.
- **Branding Editor:** Log in as `admin@acme.edu` (`Admin123!`), open the sidebar &rarr; **Branding & Theme** (`/admin/branding`) to customize tokens, preview components live, and upload brand assets.

---

## 🌐 Network Ports & Services

| Service | Address | Description |
| :--- | :--- | :--- |
| **Frontend Web App** | `http://localhost:5173` | React / Vite UI with Dark & Light theme |
| **Backend API** | `http://localhost:8000` | FastAPI server (`/docs` for OpenAPI Swagger) |
| **PostgreSQL Database** | `localhost:5433` | Multi-tenant schema with RLS (`ragplatform`) |
| **Redis** | `localhost:6379` | Token blacklists, session caching & rate limits |
| **Mailpit Web UI** | `http://localhost:8025` | Local email preview for verification & invites |

---

## 🔑 Demo & Development Accounts

All accounts are pre-seeded via `app.scripts.seed`:

### 1. Platform Superadmin (Cross-Tenant Admin)
- **Login URL:** `http://localhost:5173/platform/login`
- **Username:** `superadmin`
- **Password:** `SuperAdmin123!`

---

### 2. Tenant "Acme University" (`tenant_slug: acme`)
- **Login URL:** `http://localhost:5173/login`

| Role | Email | Password | Scope & Permissions |
| :--- | :--- | :--- | :--- |
| **Tenant Administrator** | `admin@acme.edu` | `Admin123!` | Full administrative access, custom roles, academic CRUD, rosters, teacher assignments, audit logs. Lands on `/admin`. |
| **Teacher / Faculty** | `teacher@acme.edu` | `Teacher123!` | Scoped to assigned classes and subjects. Read-only class roster access. Lands on `/teacher/dashboard`. |
| **Enrolled Student** | `student@acme.edu` | `Student123!` | Scoped strictly to enrolled cohort and subjects. Lands on `/student/dashboard`. |

---

### 3. Tenant "Default Institute" (`tenant_slug: default`)
- **Tenant Administrator:** `admin@default.edu` / `Admin123!`
- **Teacher:** `teacher@default.edu` / `Teacher123!`
- **Student:** `student@default.edu` / `Student123!`

---

## 🛡 Security & Authorization Model

1. **Default-Deny:** Every API endpoint is guarded by `require(permission, scope)`. Undecorated routes are rejected.
2. **PostgreSQL RLS:** All tenant tables enforce `tenant_id = current_setting('app.current_tenant_id')`. Even if a SQL injection or bug occurs, cross-tenant data leaks are physically blocked at the database engine level.
3. **Scoped Authorization:** Teachers can only view and manage subjects/classes assigned to them; students can only view classes they are enrolled in.
4. **Anti-Probing (404 Not Found):** Attempting to access an unassigned or foreign class ID returns a generic `404 Not Found`, never `403 Forbidden`, preventing resource ID enumeration.
5. **Privilege Escalation Defense:** Administrators cannot grant permissions they do not possess. System roles (`tenant_admin`, `teacher`, `student`) are immutable.
6. **Immutable Audit Trail:** All role assignments, class creations, enrollment modifications, and administrative operations are recorded in an append-only audit log with actor ID, IP address, and request ID. Sensitive secrets (passwords, tokens) are strictly scrubbed.

---

## 🚀 Deployment Notes

When deploying to staging or production environments, observe the following mandatory operational requirements:

1. **Reverse Proxy & Load Balancer Trust (`TRUST_PROXY`):**
   - When deploying behind a reverse proxy, CDN, or load balancer (e.g. AWS ALB, Cloudflare, NGINX), set `TRUST_PROXY=true` in the environment so that client IP addresses are accurately extracted from `X-Forwarded-For`.
   - In direct or local development setups, leave `TRUST_PROXY=false` (default) to avoid IP address spoofing in audit logs and throttle counters.

2. **Host Header Preservation (Mandatory for Multi-Tenancy):**
   - The production reverse proxy (NGINX, Caddy, Cloudflare, AWS ALB) **MUST PRESERVE** the original incoming `Host` header (e.g. `proxy_set_header Host $host;` or `X-Forwarded-Host`) when forwarding requests to the FastAPI backend.
   - The backend resolves tenant identity from the Host header (e.g. `{slug}.yourdomain.edu`). Overwriting `Host` with `localhost` or an upstream internal IP will cause tenant routing to default to the fallback tenant.

2. **Secrets Management:**
   - Never commit or store production secrets in `.env` files or repository configs.
   - All secrets (`SECRET_KEY`, `PLATFORM_SECRET_KEY`, `PLATFORM_DATABASE_URL`, `DATABASE_URL`, `REDIS_PASSWORD`) must be injected at runtime via a dedicated secrets manager (e.g., AWS Secrets Manager, GCP Secret Manager, or HashiCorp Vault).
   - In non-development environments (`ENVIRONMENT!=development`), the backend enforces that `SECRET_KEY` and `PLATFORM_SECRET_KEY` differ and are >= 32 characters, and `REDIS_PASSWORD` (or authenticated `rediss://` URL) is strictly required.

3. **Database Migrations:**
   - Execute database migrations on deploy before starting web workers:
     ```bash
     alembic upgrade head
     ```
   - Migration `0003_refresh_token_revoked_at` must run to establish the `revoked_at` column required for refresh-token theft detection and grace periods.

4. **Superadmin Account Provisioning:**
   - Initial superadmin credentials are not seeded automatically in production.
   - Run the dedicated CLI command once per environment to provision the platform superadmin:
     ```bash
     python -m app.scripts.create_superadmin --email "superadmin@yourdomain.com"
     ```
   - Passwords must meet the strong password policy (at least 12 characters, uppercase, lowercase, digit, and special symbol).

5. **Timetable Import Sandbox & Limits:**
   - Every file parse runs in a fresh subprocess (`python -m app.services.timetable_parse_child`) with a minimal environment (no `DATABASE_URL`, `SECRET_KEY`, `PLATFORM_SECRET_KEY`, `REDIS_*` or other secrets), an empty temporary working directory, input over stdin and a JSON result over stdout (capped at 5 MB). A hard 10 s timeout kills the whole process tree.
   - **CI install:** CI deliberately uses a non-editable install (`pip install ".[dev]"`) because it matches production.
   - **Import path:** the child's import path comes from where the child module lives, not from the parent's environment.
   - **Linux:** the child also applies `RLIMIT_AS` (~1 GB) and `RLIMIT_CPU` (15 s).
   - **Windows (development only):** rlimits are not available and are skipped. The other protections (clean environment, temp directory, stdin/stdout, output cap, timeout, tree kill) still apply.
   - **Production:** run the API/worker as a **non-root user** in a **container without outbound network access**. The subprocess sandbox does not restrict network or filesystem access by itself.
   - Concurrency caps and rate limits use an atomic Redis slot manager. Slots expire on their own if a worker dies. If Redis is unreachable and `ENVIRONMENT` is not `development`, imports fail closed with HTTP 503; in `development` an in-memory fallback is used and a loud error is logged.

6. **Timetable Permission Model (intentional design):**
   - A teacher who holds class-level `timetable:manage` (assigned to the class) controls the **whole class draft**, not just their own subjects. The class timetable is one shared draft.
   - Teacher double-booking and room clashes are not blocked while editing. They are reported by the pre-publish conflict check and must be acknowledged with the `confirm_conflicts` flag at publish time.
   - Teacher email addresses in pickers, templates and import previews are shown to tenant admins only; other callers see names. Import matching works by email or exact full name for everyone.
   - The tenant timezone endpoints require the dedicated `settings:manage` permission (tenant_admin only by default). The change and its audit event are one transaction.

7. **Upload Body Limits (Reverse Proxy Must Cap Too):**
   - The API rejects multipart and timetable import/template requests above 2 MB + 64 KB with HTTP 413 (`BodyLimitMiddleware`). It checks `Content-Length` and also counts the bytes actually received, so chunked requests without `Content-Length` are cut off, and it allows at most 5 multipart parts.
   - The application can only stop reading after the bytes reach it. The **reverse proxy / load balancer MUST also cap the request body** (e.g. NGINX `client_max_body_size 3m;`, Caddy `request_body { max_size 3MB }`, AWS ALB/WAF body limits) and use short body-read timeouts.
   - Upload errors return fixed messages with stable error codes. Details (exception class only, never file names, content or filesystem paths) go to the server log.
   - **PDF scan is best effort.** PDFs are rejected if their raw bytes contain `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/OpenAction` or `/AA` (after decoding `#xx` name escapes), or exceed 20,000 objects or 100 pages. It cannot see inside compressed object streams, so the real protection is the sandboxed, time- and memory-limited parser subprocess above.
   - OpenXML relationships: only plain hyperlink relationships (`.../relationships/hyperlink`) may use `TargetMode="External"`; they are never followed or fetched. Every other external target (oleObject, attachedTemplate, externalLink, frame, external images or media) is rejected.
   - OpenXML files (xlsx, docx, pptx) are typed by content (`xl/workbook.xml`, `word/document.xml`, `ppt/presentation.xml`), must match their extension, and are rejected on any `vbaProject.bin`, `/embeddings/` part, nested ZIP or OLE content, external relationship, or malformed XML.

---

---

## 📱 Frontend Design System & Responsive Conventions

The platform frontend follows a strict mobile-first architecture adhering to WCAG AA accessibility, zero inline styles, and semantic token theming.

### 1. Viewport Breakpoints
- **Mobile (375px):**
  - App shell uses `h-dvh` / `min-h-dvh` and safe-area inset padding. Page body never scrolls horizontally.
  - Sidebar collapses into an accessible slide-over drawer triggered by a 44px topbar hamburger.
  - Tables automatically transform into accessible stacked cards (`Table.tsx`), partitioning action columns into card footers and providing a mobile sort dropdown toolbar.
  - Modals and confirmation dialogs render as responsive bottom sheets with scrollable content and sticky action footers.
  - Branding Editor features a segmented control switching between form configuration and live mock preview.
- **Tablet (768px - `md:`):**
  - Topbar hamburger retains slide-over drawer ergonomics.
  - Tables transition into full data tables with horizontal scroll containment.
  - Modals transition into centered floating dialogs with side-by-side action buttons.
  - Audit log and search filters display in a responsive 2 to 4-column grid.
- **Desktop (1280px - `lg:`):**
  - Sidebar docks permanently on the left with collapse/expand toggle.
  - Branding Editor displays side-by-side configuration form and sticky live preview.

### 2. Touch Target Sizing (WCAG 2.5.5 / 2.5.8)
- Every interactive element (buttons, inputs, select menus, hamburgers, dismiss icons, table actions) guarantees a minimum **44px touch target** on coarse pointers (`@media (pointer: coarse)` in `theme.css`).
- Desktop pointers (`pointer: fine`) maintain compact, dense desktop layouts (`sm:min-h-[38px]`, `btn-sm` `sm:min-h-[32px]`).

### 3. Style Guardrails & Quality Enforcers
- **Zero Inline Styles:** Never use `style={{...}}` or `style={...}` in React components. All styling must use Tailwind utility classes or semantic CSS classes defined in `@layer components` within `theme.css`.
- **Zero Direct DOM Style Mutations:** Never mutate `element.style` (e.g. `document.body.style.overflow = "hidden"`). Body scroll locking is managed via the `.body-scroll-lock` class toggled cleanly by the `useScrollLock` hook.
- **No Hardcoded Hex Colors:** Component code must never contain raw hex codes (e.g. `bg-[#1e293b]`). Hex colors are strictly restricted to an exact allow-list of theme definitions and branding algorithms (`src/styles/theme.css`, `src/features/branding/utils/tokens.ts`, `src/features/branding/types.ts`).
- **Automated Style Scanner:** Run `npm run check:styles` to verify zero violations across the codebase. This check runs automatically on every `npm run lint` and in CI.

## 🧪 Running Automated Tests

Run backend test suite:
```bash
# Run Phase 2 & RBAC security test suite:
pytest backend/tests/test_phase2_endpoints.py backend/tests/test_authz_rbac.py -v

# Run all backend tests:
pytest backend/tests/ -v
```

Run frontend verification:
```bash
cd frontend
npm run test          # Unit tests (theme engine, style guardrails, timetable helpers, API schemas)
npm run check:styles  # Style rule guardrail scanner (zero inline styles, zero hardcoded colors)
npm run typecheck     # TypeScript strict type checking
npm run lint          # ESLint + check:styles
npm run build         # Vite production build
```

### 4. Running Playwright End-to-End Smoke Tests
The e2e suite drives a real browser (Chromium, headless) against the local dev stack and the seeded demo data. It creates and publishes timetable entries, so it only runs against a local stack: the config refuses any non-local base URL or `NODE_ENV=production`.

1. Start infrastructure, migrate and seed, then start the backend yourself (port 8000):
   ```bash
   docker compose up -d
   cd backend && alembic upgrade head && python -m app.scripts.seed
   uvicorn app.main:app --port 8000
   ```
2. Install the browser once. This is a manual step, nothing is downloaded automatically:
   ```bash
   cd frontend && npx playwright install chromium
   ```
3. Run the suite. Playwright starts the frontend dev server (port 5173) itself, or reuses one that is already running:
   ```bash
   cd frontend
   npm run e2e
   ```
The suite (`frontend/e2e/smoke.spec.ts`) covers:
- Admin, teacher and student on `acme` opening every sidebar page with no uncaught page error and no console error.
- A student typing `/admin` sees the 403 page.
- Teacher creates a draft entry and publishes it; the student sees it and the Next lecture card.
- Import wizard with a small valid CSV, and a hostile file rejected with a friendly message.
- Backend unreachable (requests blocked): friendly error, not a blank screen.

Each run adds a small `E2E-...` lecture to Class 10-A and a draft to Class 10-B. Re-seed the database to reset.

**CI:** e2e is not part of CI. It needs Postgres, Redis, migrations, seed data, the backend and a browser download, which does not fit the roughly 3 minute budget. It stays a manual script.

