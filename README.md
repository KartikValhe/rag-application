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

#### Recreating the dev database (required once after the move to the pgvector image)
Postgres now runs `pgvector/pgvector:0.8.7-pg16-bookworm` (Debian, glibc) instead of `postgres:16-alpine` (musl). The two sort text differently, so the old `pgdata` volume must not be reused.

> **WARNING: this deletes ALL local dev data** (every table in `ragplatform` and `ragplatform_test`, plus the Redis volume). Nothing is recoverable.

Run from the repository root, in this order:
```bash
docker compose down -v
docker compose up -d
# wait until `docker compose ps` shows postgres as healthy
cd backend
alembic upgrade head
python -m app.scripts.seed
python -m app.scripts.create_superadmin --email "superadmin@example.com"   # use your own address; see Deployment Notes
```
Check the extension is available: `docker exec rag-postgres psql -U postgres -c "select name, default_version from pg_available_extensions where name='vector'"` (expect `0.8.7`).

### 3. Backend Setup
Activate the virtual environment and install dependencies:
```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

pip install ".[dev]"
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
   - The API rejects oversized requests with HTTP 413 (`BodyLimitMiddleware`; limits in a registry keyed by method + path regex in `app/core/body_limit.py`). Upload routes (branding, CSV imports, timetable import) are limited to 2 MB + 64 KB and 5 parts. Only `POST /api/v1/documents` and `POST /api/v1/documents/{uuid}/versions` get the document ceiling, 50 MB + 64 KB and 6 parts (the tenant's own, lower file limit is enforced by the route); every other request under `/api/v1/documents` is limited to 64 KB. Bytes are counted as they arrive, so a body longer than its `Content-Length` is cut off.
   - **Before any body is read** the middleware refuses a repeated `Content-Type` or `Content-Length` (400, every route), a malformed `Content-Length` (400), and on upload routes a missing or invalid Bearer token (401), a non-multipart body (415), a missing `Content-Length` (411, so chunked uploads are refused) and more than 4 uploads in flight per client IP (429 with `Retry-After`; Redis down outside development gives 503). The per-IP cap uses the same client IP as the throttles, so set `TRUST_PROXY` correctly behind a proxy, or every user shares the proxy's IP. The proxy must pass `Content-Length` through and must not merge or duplicate `Content-Type`/`Content-Length` headers.
   - The application can only stop reading after the bytes reach it. The **reverse proxy / load balancer MUST also cap the request body** (e.g. NGINX `client_max_body_size 3m;`, Caddy `request_body { max_size 3MB }`, AWS ALB/WAF body limits) and use short body-read timeouts.
   - Upload errors return fixed messages with stable error codes. Details (exception class only, never file names, content or filesystem paths) go to the server log.
   - **PDF scan is best effort (one rule for timetable imports and documents).** PDFs are rejected if their raw bytes contain `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile`, `/RichMedia` or `/XFA` (after decoding `#xx` name escapes, so `/J#61vaScript` is caught), or exceed 20,000 objects or the page cap (timetable 100, documents 200 by default). `/OpenAction` and `/AA` are allowed: they only name actions and nothing here renders or executes a PDF. **The byte scan only sees uncompressed names**: a name inside a compressed object stream is invisible to it, so the real protection is the sandboxed, time- and memory-limited parser subprocess above, not the scan.
  - **Document store (private).** Original document uploads go to a separate private store (`DOCUMENT_STORE_DIR`, default `document_store/`; it must not be inside or contain `UPLOAD_DIR`, the public branding directory). Keys are `t/{tenant_id}/d/{random hex}`, there is no URL method and no route serves the directory; downloads are streamed through an authenticated route. Back up and protect this directory like the database. Supabase Storage replaces the local backend later behind the same interface.
   - OpenXML relationships: only plain hyperlink relationships (`.../relationships/hyperlink`) may use `TargetMode="External"`; they are never followed or fetched. Every other external target (oleObject, attachedTemplate, externalLink, frame, external images or media) is rejected.
   - OpenXML files (xlsx, docx, pptx) are typed by content (`xl/workbook.xml`, `word/document.xml`, `ppt/presentation.xml`), must match their extension, and are rejected on any `vbaProject.bin`, `/embeddings/` part, nested ZIP or OLE content, external relationship, or malformed XML.

### Worker deployment requirements

The document ingestion worker parses untrusted files in a child process (`app/core/sandbox.py`). The child has a clean environment, an empty working directory, a time limit and (Linux) memory, CPU and parent-death limits, **but it runs as the same user as the worker and the operating system gives it no filesystem or network confinement of its own.** In production the worker therefore MUST run like this:

- **In a container, as a non-root user** (root can read every `/proc` file). Both the worker and its child set `PR_SET_DUMPABLE` to 0 at startup (Linux), so the child cannot read `/proc/<worker pid>/environ`, where the database URLs and `SECRET_KEY` live.
- **Secrets from environment variables or a secret manager, and NO `.env` file inside the image** (add it to `.dockerignore`). Nothing a parser exploit could read should be in the image.
- **The child must not be able to read the backend source or other data it does not need:** install the package into site-packages (non-editable wheel; the child's import path then holds no backend folder), use a read-only root filesystem and mount only what is needed (the document store directory, a scratch directory). The child is the same user, so it can still read what the worker reads (including every tenant's stored files) until it runs as a separate user or in its own container (backlog B44).
- **Memory, CPU and process limits from the container (cgroup)**: `--memory`, `--cpus`, `--pids-limit`. The child's `RLIMIT_AS`/`RLIMIT_CPU` are only a second layer, and Linux only.
- **No outbound network for the worker** except PostgreSQL, Redis and (later) object storage.

**Windows and macOS have none of these protections** (no non-dumpable flag, no rlimits, no parent-death signal, no container). Use them for development only.

### Parser dependencies, and what `pip-audit` cannot see

The libraries that parse untrusted files are **pinned to exact versions** in `backend/pyproject.toml`: `pypdfium2`, `python-docx`, `lxml`, `Pillow` (base dependencies, because the API image also runs the timetable import children and validates branding images) and `python-pptx` (document parser only). The `parser` extra lists the full set the sandboxed document parser needs (`pip install ".[parser]"`; `".[dev]"` includes it). The API **process** never imports `pypdfium2`, `python-docx`, `python-pptx` or `lxml` (`tests/test_review_5b3_deps.py` imports the app with them blocked, and again without the blocker).

**`pip-audit` cannot see PDFium CVEs.** `pypdfium2` wheels bundle a compiled PDFium; the PyPI advisory database that `pip-audit` reads does not list PDFium's own vulnerabilities (they are published as Chromium/PDFium security fixes). A clean `pip-audit` therefore says nothing about the bundled PDFium. **Review the pinned `pypdfium2` version on each release**: check which PDFium build the wheel contains and whether a newer wheel carries security fixes, then bump the pin together with the test run.

### Embedding model (operator steps; step 5c-1)

The embedder (`backend/app/ml/`, design A1) runs **multilingual-e5-small** locally through ONNX Runtime: no torch, no `transformers`, no `huggingface_hub`, and no student or document text leaves the system. The model files are **never downloaded by the API, the worker or the tests**. Exactly one operator command downloads them, and everything else reads verified local files.

The pinned model (repository, commit, file names, sizes and sha256) is in `backend/app/ml/models.lock.json`; `app/ml/model_keys.py` keeps the key stored in the database. Install the libraries with the `ml` extra (`pip install ".[ml]"`; `".[dev]"` includes it; the API image needs it only because the API embeds queries lazily).

```bash
# 1. Download and verify (from backend/). About 490 MB. Run it on a build or admin machine, not at runtime.
python -m app.scripts.fetch_model e5-small --out ./models
#    -> ./models/e5-small/onnx/model.onnx and ./models/e5-small/onnx/tokenizer.json
#    HTTPS only, redirects only to huggingface.co and its CDN hosts, size and sha256 checked against the lock file,
#    the file is moved into place only when it matches; on a mismatch the download is deleted and the command exits non-zero.

# 2. Point the app at the directory (settings, see .env.example)
#    EMBEDDING_MODEL_DIR=./models      EMBEDDING_MODEL_ID=e5-small
#    EMBEDDING_BATCH_SIZE=8            EMBEDDING_THREADS=1

# 3. Optional manual checks (not tests, not in CI)
python -m app.scripts.check_onnx_parity     # cosine > 0.999 against a reference on 50 en/hi/mr sentences
python -m app.scripts.bench_embed           # texts/s, tokens/s and peak RSS for batch sizes 1, 8, 16, 32
RUN_REAL_MODEL=1 pytest tests/test_ml_real_model.py   # model-quality checks on the real files (skipped by default)
```

- **At start-up** the loader re-verifies the size and sha256 of every file (a second or two for 470 MB), refuses an extra file, a symlink or an entry marked `UNVERIFIED`, and then loads only the ONNX protobuf and `tokenizer.json`. A missing or changed file stops the service with a clear message. It never opens a network connection.
- **Memory and speed (measured 2026-11-18 on a Windows laptop, real e5-small, `intra_op_num_threads=1`; the peak RSS includes the benchmark process itself):** batch 1: 3.5 texts/s at 1,094 MB; batch **8: 3.2 texts/s at 1,369 MB (the default)**; batch 16: 2.7 texts/s at 1,447 MB; batch 32: 3.0 texts/s at 1,818 MB. A bigger batch is not faster and costs memory. A 300-chunk document takes about 100 s to embed, a 1,000-chunk upload about 5 minutes. The API loads the model lazily on the first query, once per process, so run few API workers (`WEB_CONCURRENCY` 1-2). Plan about 1.5 GB for a worker process while it embeds (the model, the activations and the sandboxed parser child), and keep `EMBEDDING_BATCH_SIZE=8` on a 2 GB host.
- **Production images bake the verified files in** (`COPY` the directory from a build stage that ran `fetch_model`) and run with **no outbound network** for this purpose; keep the directory read-only for the service user. Nothing in the image build or at runtime may call Hugging Face except that one build step.
- **A new or changed model** (for example `bge-m3`, still `UNVERIFIED` in the lock file) is first reviewed with `python -m app.scripts.fetch_model <name> --print-hashes [--file PATH ...]`, which prints the size and sha256 of the files you already have and writes nothing, and then pinned in the lock file by hand in a reviewed change.
- `fetch_model` has no proxy support (`trust_env` is off on purpose, so a `~/.netrc` or proxy variable cannot add credentials). If a redirect to a host outside the allow-list is refused, the host is printed: review it before adding it to `ALLOWED_HOST_SUFFIXES`.

### Running the ingestion worker locally (step 5c-2)

The worker (`python -m app.workers.ingest`, from `backend/`) turns uploaded files into searchable chunks: sandboxed validation and parsing, chunking, embedding and one short indexing transaction, plus the sweeper loop. It **starts only when everything it needs is there**; otherwise it prints what is missing and exits with code 2:

- `WORKER_DATABASE_URL` (a direct or session-mode connection as the `app_worker` role), `REDIS_URL` and `DOCUMENT_STORE_DIR` (the same directory the API uses);
- `EMBEDDING_MODEL_DIR` pointing at the verified model files (`python -m app.scripts.fetch_model e5-small --out ./models`, see above). The loader re-verifies every file's size and sha256 at start-up (a second or two) and loads the model **once**; a missing or changed file stops the start. There is no setting or environment variable that selects a fake model.

```bash
docker compose up -d                      # Postgres, Redis
alembic upgrade head && python -m app.scripts.seed
python -m app.scripts.fetch_model e5-small --out ./models      # once; the only command that uses the network
EMBEDDING_MODEL_DIR=./models python -m app.workers.ingest      # one worker; Ctrl+C finishes or releases the current job
```

- **Memory:** about 1.4-1.5 GB while a job embeds with the default batch size 8 (see the measurements above); the parser child is capped separately (`DOC_PARSE_MEMORY_LIMIT_MB`, Linux only). Do not run it on a host with less than 2 GB.
- **Time:** about 3 texts/s on one thread, so a 300-chunk document takes about 100 s and a 1,000-chunk upload about 5 minutes. The job lease (`INGEST_LEASE_SECONDS`, 180) is renewed every `INGEST_HEARTBEAT_SECONDS` (30) and after every embedding batch, and one attempt may run up to `INGEST_MAX_JOB_SECONDS` (3600), so a long upload does not lose its lease. The blocking model call runs on its own thread, so the heartbeat keeps ticking.
- **Publishing:** a finished version is `ready`. Publish it with `POST /api/v1/documents/{id}/versions/{version_id}/publish` (the previous published version is archived in the same transaction). **Auto-publish is off** (`DOC_AUTO_PUBLISH_ENABLED=false`) until the injection scanner of step 5d exists; do not switch it on before then.
- Production runs the worker in a container as described under "Worker deployment requirements" above.

### Document parser benchmark (manual, real files never committed)

`python -m app.scripts.bench_parse <folder>` (from `backend/`) parses every PDF, DOCX, PPTX, TXT and MD file in a folder exactly as the worker does (same sandboxed child, same parent-side checks) and prints per file: format, pages, characters, script guess, gate result, seconds and peak memory. It needs no database, Redis or settings. Put real syllabus files in `backend/tests/fixtures/real_corpus/`: that folder is in `.gitignore`, and **real documents are never committed** (they may hold personal or copyrighted content). It is not a test and does not run in CI.

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

### Test database

pytest **always** runs against a dedicated `<name>_test` database (`ragplatform_test` locally), never the dev database. `tests/conftest.py` rewrites `DATABASE_URL`, `PLATFORM_DATABASE_URL`, `WORKER_DATABASE_URL` and `MIGRATION_DATABASE_URL` to the `_test` name and aborts the whole session unless every URL is a `_test` database on localhost, 127.0.0.1 or a docker service name and `ENVIRONMENT=development`. Tests never run against dev, staging or Supabase.

```bash
cd backend
python -m app.scripts.prepare_test_db            # check roles/pgvector, migrate the test DB to head (idempotent)
python -m app.scripts.prepare_test_db --reset    # drop and recreate the test DB first
```

> **Warning:** `--reset` drops the test database. pytest stops with a one-line instruction when the test DB is not at the migration head. The Playwright e2e suite and manual use keep the dev database (`ragplatform`); only pytest uses the test database. CI already uses `ragplatform_test`, which the guard accepts.

### Backend test tiers

Every backend test file belongs to a tier (one reviewed table: `backend/tests/tier_map.py`; a new test file without an entry fails collection). One command runs a tier, never two runs at once, and prints pytest's summary line as its last line:

```bash
cd backend
python -m app.scripts.run_tests fast   # no Postgres, Redis or child process; works with docker stopped (about 40 s)
python -m app.scripts.run_tests gate   # fast + security (tenant isolation, authorization, hostile input, sandbox) (about 6 min)
python -m app.scripts.run_tests gate --touched   # ... plus the tests of the modules changed in the working tree
python -m app.scripts.run_tests full   # everything (about 17 min): CI, or on request
python -m app.scripts.run_tests full tests/test_isolation.py -k worker   # any pytest arguments are passed on
```

Markers: `fast`, `db` (Postgres and/or Redis), `subprocess`, `security`, `slow` (a file of 30 s or more), `linux` (tests skipped off Linux; they run in CI). Timings are from a Windows developer machine with docker on localhost. `real_model` tests need the real model files and `RUN_REAL_MODEL=1`; they are skipped by default.

Plain `pytest` still works and runs everything (this is what CI does). Run a single file with `pytest tests/test_x.py`.

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

