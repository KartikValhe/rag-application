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
npm run typecheck
npm run lint
npm run build
```

