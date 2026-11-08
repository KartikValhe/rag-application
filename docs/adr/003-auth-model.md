# ADR 003: Identity, Authentication, and Session Model

## Status
Accepted

## Context
Multi-tenant identity requires clear boundaries, safe token management, and protection against unauthorized credential sharing and brute-force attacks.

We evaluated:
1. **Application-managed JWT access token + rotating HttpOnly refresh tokens** (argon2id passwords, local verification).
2. **Supabase Auth / Third-party BaaS Auth**.
3. **Session cookies in Redis**.

## Decision
We chose **Option 1: Short-lived JWT access tokens + rotating hashed refresh tokens with HttpOnly cookies**, managed directly by FastAPI.

### Architectural Rules:
1. **User Identity Boundary**: A user belongs strictly to one tenant (`users.tenant_id`). Email is unique per tenant, not globally. The same human at two different colleges has two distinct, isolated accounts.
2. **Platform Superadmins**: Superadmins live in an isolated `platform_admins` table signed with an entirely separate cryptographic secret (`PLATFORM_SECRET_KEY`).
3. **Short-Lived Access Token**: Signed with HS256, carrying `tenant_id`, `sub`, and `is_admin`, with a 15-minute TTL.
4. **Rotating Refresh Token**: URL-safe 48-byte token stored hashed (SHA-256) in `refresh_tokens`, transported via an `HttpOnly`, `SameSite=Lax`, `Secure` cookie. Each refresh issues a new token and revokes the old one.
5. **Brute Force Protection**: Failed login attempts are throttled via Redis sliding-window counters (5 attempts per 15-minute window before HTTP 429 lockout).
6. **Cross-Tenant Validation**: Access tokens are strictly matched against the tenant resolved from the request's host/subdomain. A token issued for Tenant A is rejected on Tenant B's host.

