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
Smoke tests run in headless mode against the local development stack with seeded demo data:
1. Ensure the backend and frontend dev servers are running with seeded demo data:
   ```bash
   # Terminal 1: backend (http://localhost:8000)
   cd backend && uvicorn app.main:app --reload --port 8000

   # Terminal 2: frontend (http://localhost:5173)
   cd frontend && npm run dev
   ```
2. Execute the headless Playwright smoke tests:
   ```bash
   cd frontend
   npm run test:e2e
   ```
The smoke suite verifies:
- Admin, teacher, and student authentication on tenant `acme`.
- Successful navigation across all sidebar pages with zero console errors or uncaught page exceptions.
- Faculty timetable publishing and real-time student schedule synchronization.
- Role-based route protection (students navigating to `/admin` receive the 403 Forbidden state).
- White-label brand isolation showing distinct branding tokens on second tenant host (`default` vs `acme`).

