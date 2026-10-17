# ADR-012: Authentication, authorization, rate limiting, CI/CD and deployment

**Status:** Accepted (Phase 8).

Related: ADR-002 (Postgres is the source of truth), ADR-003 (Docker isolation), ADR-007 (evaluation secrecy), ADR-010 (observability).

## Context

Through Phase 7 the API is open to anyone who can reach its port; compose binds everything to 127.0.0.1 to compensate. Phase 8 makes the platform safe to run as a shared service on one host: callers are identified, their permissions are checked, abusive clients are throttled, every change is tested automatically, and there is a documented production deployment. This is still a research/portfolio system, not a hardened multi-tenant boundary (README "Security model").

## Decision

### A. API keys (authentication)

- **Why API keys, not OAuth/JWT sessions:** the clients are scripts, CI jobs and one dashboard; there is no user directory to federate with. Keys are simple to issue, rotate and revoke, and need no external identity provider.
- **Format:** `ae_<8-char prefix>_<32 bytes base64url secret>`, shown once at creation. Stored as `api_keys (id, name, prefix UNIQUE, secret_sha256, scopes text[], created_at, last_used_at, expires_at NULL, revoked_at NULL)`. SHA-256 is enough because the secret is 256 bits of randomness (no dictionary to attack); the comparison is constant-time (`hmac.compare_digest`).
- **Transport:** `Authorization: Bearer <key>`. Missing or invalid → 401 with `WWW-Authenticate: Bearer`; revoked/expired → 401. `last_used_at` is updated at most once a minute per key (no write per request).
- **Bootstrap:** `python -m agenteval_api.keys create --name <n> --scopes read,write,admin` (and `list`, `revoke`), run inside the API container. No anonymous key-creation endpoint exists. `POST/GET/DELETE /api/v1/keys` require `admin`.
- **Toggle:** `AGENTEVAL_AUTH_ENABLED` (default **true**). Tests and e2e use a key created by their fixtures; only an explicit `false` (documented as local-only) disables checks.

### B. Scopes (authorization)

| Scope | Allows |
|---|---|
| `read` | every `GET` under `/api/v1` |
| `write` | creating agents/versions/tasks/datasets/runs/evaluations, cancel, replay, freeze |
| `admin` | key management |

One dependency per route declares its scope; a test sweeps every route in the OpenAPI schema and fails if any `/api/v1` route lacks one. `/health` and `/ready` stay open. `/metrics` stays unauthenticated but is meant for the internal network only (compose does not publish it beyond 127.0.0.1; the production profile publishes nothing but the reverse proxy). The platform is single-tenant: scopes, not per-object ownership.

### C. Rate limiting

- Redis, fixed window per minute with an atomic `INCR` + `EXPIRE` (one round trip via a tiny Lua script), keyed by API key id (or client IP when auth is disabled). Separate budgets for reads and writes (`AGENTEVAL_RATE_LIMIT_READ_PER_MIN` 600, `AGENTEVAL_RATE_LIMIT_WRITE_PER_MIN` 120).
- Over the limit → 429 with `Retry-After` and `X-RateLimit-Limit/Remaining/Reset`.
- **Fail open** if Redis is unreachable (logged + `agenteval_rate_limit_errors_total`): Redis is a coordination aid here (ADR-001), never the authority, and an outage must not take reads down.

### D. Dashboard

The dashboard asks for an API key on first use and keeps it in `sessionStorage` (cleared when the tab closes), sending it as a bearer token. CORS allows the `Authorization` header for the configured origins only. A read-only key works for browsing; cancel/replay need `write`.

### E. CI/CD (GitHub Actions)

- `ci.yml` on every push and pull request: lint (ruff, format, mypy), unit tests, integration tests (Docker is available on `ubuntu-latest`; testcontainers starts Postgres/Redis), the dashboard checks (`npm ci`, typecheck, lint, test, build), dependency audit (`pip-audit`, `npm audit --omit=dev`), and image builds.
- `e2e.yml` on pushes to `main` and nightly: builds the sandbox image, brings up compose, runs `make e2e`.
- `release.yml` on version tags: builds and pushes `api`, `worker` and `web` images to GHCR.

### F. Deployment

`docker-compose.prod.yml` (used with the base file): no bind-mounted source, no `--reload`/`watchfiles`, `restart: unless-stopped`, secrets from an `.env` file outside the repo, resource limits, only a reverse proxy (Caddy, automatic TLS) published; Postgres/Redis/metrics stay on the internal network. `docs/deployment.md` covers first deploy, migrations (stop workers → migrate → start), key bootstrap, backups (Postgres dump + the artifact volume together, ADR-008), upgrades and rollback.

### G. Security review

`docs/security-review.md`: threat model (untrusted agent code, untrusted API callers, a compromised worker), the controls per threat, findings from a review of the code and configuration with their fixes or accepted risks (notably: the worker mounts the Docker socket, which is root-equivalent on the host), and the residual risks.

## Consequences

- Every API client must send a key; existing tests and e2e get fixtures that create one.
- One new table (migration) and a small amount of Redis traffic per request.
- CI time grows (integration tests need Docker); e2e runs only on `main` and nightly.

## Alternatives considered

| Option | Why not |
|---|---|
| OAuth2 / OIDC | Needs an identity provider; no human-user directory in scope |
| JWTs signed by the API | Revocation needs a deny-list anyway; opaque keys checked against Postgres are simpler |
| Rate limiting in a reverse proxy only | Can't key by API key without auth at the proxy; the app limiter is shared by every deployment shape |
| bcrypt/argon2 for key hashes | Designed for low-entropy passwords; a 256-bit random secret makes SHA-256 sufficient and keeps per-request cost negligible |
