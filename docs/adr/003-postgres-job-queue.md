# ADR 003: Postgres Job Queue

## Status
Accepted (2026-11-15)

## Decision
Background work (document ingestion, re-index, sweeper) runs from a **Postgres job table** (`app.ingest_jobs`). Workers claim rows with `SELECT ... FOR UPDATE SKIP LOCKED`, hold a lease, and renew it with a heartbeat. ARQ and a Redis job queue are not used. Redis stays for rate limits, slot leases and caches.

## Why
- **Atomic with the data.** The API inserts the job in the same transaction as the document version, so there is no state where a version exists without a job or the reverse.
- **Fewer moving parts.** One datastore to back up and reason about; no second durable store whose loss silently drops jobs.
- **Small scale.** Pilot volume is a few uploads per hour; polling every few seconds is enough.
- **Recovery is plain SQL.** An expired lease makes the row claimable again, so a killed worker recovers without extra code. Retries use `attempts` and `run_after`.

## RLS interaction
- `ingest_jobs` has a `tenant_id`, `ENABLE` + `FORCE ROW LEVEL SECURITY` and the usual `tenant_isolation` policy, so the API (`app_tenant`) can only enqueue for its own tenant.
- A narrow `app_worker` role (NOSUPERUSER, NOBYPASSRLS) has a separate `worker_claim` policy on that table only, plus `SELECT (id)` on `app.tenants` (which has no RLS) for the sweeper. It has no grant on documents, chunks, users or other tenant tables.
- The worker does the real work in a separate `app_tenant` session. It sets `app.tenant_id` from the `tenant_id` of the claimed row (never from a payload), and again in every transaction.

## Residual risk
- The worker process holds both credentials. A bug that passes the wrong job to `tenant_session_for` is contained to that job's tenant by RLS but not prevented; tests cover this.
- `app_worker` sees job metadata (ids, status, timestamps) across tenants.
- Polling adds a little latency and constant light load; many workers on a hot table could contend (SKIP LOCKED limits this).
- No built-in scheduling, priorities or dashboards; we write what we need.

## How to swap later
All access goes through `workers/queue.py` (enqueue, claim, heartbeat, complete, fail). To move to ARQ, Celery or a cloud queue, reimplement that module and keep the Postgres row as the source of truth for status, or dual-write during migration. Job handlers take `(tenant_id, version_id)` and do not depend on how they were delivered. Trigger to revisit: sustained queue depth or claim latency that polling cannot meet, or a need for fan-out workloads beyond ingestion.
