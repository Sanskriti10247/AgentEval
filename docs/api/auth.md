# Authentication, scopes, rate limits and key management (Phase 8)

The design and its alternatives are in [ADR-012](../decisions/ADR-012-production-polish.md) (A to D). This page is the behaviour reference. All paths are under `/api/v1`; errors are RFC 7807-style `application/problem+json`: `{type, title, status, detail}`.

## Sending a key

Every `/api/v1` request carries an API key as a bearer token:

```bash
curl -s -H "Authorization: Bearer $AGENTEVAL_API_KEY" localhost:8000/api/v1/runs
```

`/health`, `/ready` and `/metrics` need no key. `/metrics` is unauthenticated on purpose and is meant for the internal network only (the compose file binds the API to `127.0.0.1`; the production profile publishes nothing but the reverse proxy). `/docs` and `/openapi.json` are served without a key too: they describe the API, not its data. The OpenAPI schema declares the bearer scheme, so the Swagger "Authorize" button works.

A key looks like `ae_<8 hex>_<43 characters>`. Only its SHA-256 is stored; the full key is shown once, when it is created, and cannot be recovered (create a new one and revoke the old). The 8-character **prefix** is not secret: it identifies the key in `list`, in logs and in `revoke`.

## Getting the first key

There is no anonymous way to create a key. An operator mints the first one with the CLI, run where the database is reachable (normally inside the API container):

```bash
docker compose exec api python -m agenteval_api.keys create --name me --scopes read,write,admin
docker compose exec api python -m agenteval_api.keys create --name ci --scopes read,write --expires-days 90
docker compose exec api python -m agenteval_api.keys list
docker compose exec api python -m agenteval_api.keys revoke <prefix>
```

`create` prints the key once. With an admin key you can also manage keys over HTTP:

| Method | Path | Result |
|---|---|---|
| POST | `/keys` | body `{name 1-100, scopes [read, write, admin], expires_days? 1-3650}`. **201** with the key's fields plus `key` (the only time it is returned); **422** on a bad body |
| GET | `/keys` | **200**, every key oldest first: `id, name, prefix, scopes, created_at, last_used_at, expires_at, revoked_at`. Never the secret or the hash. Revoked keys stay listed |
| DELETE | `/keys/{prefix}` | **204**, idempotent (a repeat is 204 again; the first `revoked_at` stands); **404** for an unknown prefix |

## Scopes

Scopes are independent (`write` does not imply `read`); give a key every scope it needs.

| Scope | Allows |
|---|---|
| `read` | every `GET` under `/api/v1` |
| `write` | creating agents, versions, tasks, datasets, runs and evaluations; freezing datasets; cancelling and replaying runs and evaluations |
| `admin` | the `/keys` endpoints |

The platform is single-tenant: scopes, not per-object ownership. A read-only key is what the dashboard needs to browse; cancel and replay need `write`.

## Responses

| Status | When | Notes |
|---|---|---|
| 401 | no `Authorization` header, not the `Bearer` scheme, a malformed or unknown key, a wrong secret, a **revoked** key, an **expired** key | always `WWW-Authenticate: Bearer`. The detail says `revoked` or `expired` only for a key whose secret was correct |
| 403 | a valid key without the route's scope | `detail`: `This API key lacks the write scope` |
| 429 | over the key's budget | `Retry-After` plus the `X-RateLimit-*` headers |

Authentication is checked first, then the rate limit, then the scope: a key hammering routes it may not use is throttled like any other.

## Rate limits

A fixed window of one minute per API key (per client IP when authentication is off), counted in Redis with one atomic `INCR` + `EXPIRE`. There are two budgets: **read** for `GET`, `HEAD` and `OPTIONS`, **write** for everything else.

| Setting | Default | |
|---|---|---|
| `AGENTEVAL_RATE_LIMIT_READ_PER_MIN` | 600 | `0` disables the read budget |
| `AGENTEVAL_RATE_LIMIT_WRITE_PER_MIN` | 120 | `0` disables the write budget |

Every rate-limited response (success or 429) carries `X-RateLimit-Limit`, `X-RateLimit-Remaining` and `X-RateLimit-Reset` (seconds until the window ends); a 429 adds `Retry-After` (the same number). A fixed window allows a burst of up to twice the budget across a window boundary: the limit protects the service from runaway clients, it is not a billing meter.

**Fail open.** Redis is a coordination aid, never an authority (ADR-001). If it cannot be reached the request is allowed, a `rate_limit_unavailable` warning is logged and `agenteval_rate_limit_errors_total` is incremented. `agenteval_auth_failures_total{reason}` (`missing`, `invalid`, `revoked`, `expired`, `forbidden`) and `agenteval_rate_limited_total{kind}` count refusals.

Benchmarks and load tests submit far more than 120 writes a minute: run the stack with `AGENTEVAL_RATE_LIMIT_WRITE_PER_MIN=0` (see `benchmarks/README.md`).

## Authentication off

`AGENTEVAL_AUTH_ENABLED=false` (default `true`) is a local-only convenience: every caller is anonymous with all three scopes (key management included), any `Authorization` header is ignored, and the budgets are keyed by the client address the socket shows. Behind a reverse proxy that address is the proxy's, so do not run a shared deployment this way. The API logs an `auth_disabled` warning at startup.

## Logs

Request lines carry the key's `api_key_prefix` and `api_key_name` (bound to the log context for the request). The key itself never reaches a log: the redaction processor replaces `ae_...` keys and `Bearer <token>` text in any value, and the value of any `authorization` field, with `[REDACTED]`.
