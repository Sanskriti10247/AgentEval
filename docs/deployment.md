# Deployment

How to run AgentEval on a single dedicated Linux host with Docker Compose (ADR-012 F). This is a
research and portfolio deployment, not a hardened multi-tenant platform: read
[The Docker socket](#the-docker-socket) before you expose it.

What runs:

| Service | Published | Notes |
|---|---|---|
| `caddy` | 80, 443 | TLS (automatic certificates for `AGENTEVAL_DOMAIN`), proxies `/api/*`, `/health`, `/ready` to the API and everything else to the dashboard. `/metrics` is answered 404. |
| `api` | no | uvicorn, no reload |
| `web` | no | Next.js standalone server |
| `worker` | no | `python -m agenteval_worker.main`; holds the Docker socket and the artifact volume |
| `postgres`, `redis` | no | passwords required; data in named volumes |
| `prometheus`, `jaeger` | loopback only | optional, `--profile observability` |

Sandbox containers are not compose services: the worker creates them on demand as siblings
through the Docker socket.

## Prerequisites

- A Linux host dedicated to AgentEval, with Docker Engine and Compose v2 (2.24 or newer: the
  override file uses `!reset` and `!override`). See [the Docker socket](#the-docker-socket) for why dedicated.
- A DNS name pointing at the host, and ports 80 and 443 reachable from the internet (Caddy
  answers the ACME challenge itself). Nothing else should be published.
- The repository checked out on the host (compose files, `infrastructure/caddy/Caddyfile`, and
  Dockerfiles if you build locally).
- Sizing: see [Sizing](#sizing).

## First deploy

All commands run from the repository root. Define a shorthand for the two files and the env file:

```bash
export AEC='docker compose --env-file /etc/agenteval/prod.env -f docker-compose.yml -f docker-compose.prod.yml'
```

1. **Create the env file outside the repository.**

   ```bash
   sudo install -d -m 700 /etc/agenteval
   sudo install -m 600 .env.prod.example /etc/agenteval/prod.env
   sudoedit /etc/agenteval/prod.env
   ```

   Set `AGENTEVAL_DOMAIN`, generate `POSTGRES_PASSWORD` and `REDIS_PASSWORD` (letters and digits,
   for example `openssl rand -base64 36 | tr -d '/+=' | cut -c1-40`), and pin
   `AGENTEVAL_VERSION` to a release tag. Never commit this file. `make prod-config` checks that
   the compose files parse (with throwaway secrets).

2. **Get the images**, either of:

   - *Pull the release images* (published by `release.yml` on a `v*` tag):
     `$AEC pull api worker web`. The published `web` image bakes in the API address given when it
     was released (repository variable `AGENTEVAL_WEB_API_URL`, default `http://localhost:8000`),
     so it only works at that address. For any other domain, build `web` on the host instead.
   - *Build on the host*: `$AEC build` (this builds `api`, `worker` and `web` with the domain
     baked into the dashboard as `https://$AGENTEVAL_DOMAIN`).

3. **Provide the sandbox image under the allow-listed name.** The worker only runs images in
   `AGENTEVAL_IMAGE_ALLOWLIST` (default `agenteval-sandbox:py312`), and it must exist on the host's Docker daemon:

   ```bash
   docker build -t agenteval-sandbox:py312 infrastructure/docker/sandbox          # or:
   docker pull ghcr.io/<owner>/agenteval-sandbox:<tag> && \
     docker tag ghcr.io/<owner>/agenteval-sandbox:<tag> agenteval-sandbox:py312
   ```

4. **Start the data stores and migrate, with the workers stopped.**

   ```bash
   $AEC up -d --wait postgres redis
   $AEC run --rm api alembic -c packages/core/alembic.ini upgrade head
   ```

   The first start of `api`/`worker` before this step fails (the schema is missing); that is expected.

5. **Start everything.**

   ```bash
   $AEC up -d --wait
   $AEC ps
   curl -fsS https://$AGENTEVAL_DOMAIN/ready
   ```

   The first request triggers certificate issuance and can take a few seconds. If it fails, read
   `$AEC logs caddy`: the usual causes are DNS not pointing at the host or port 80 blocked.

6. **Bootstrap an admin key.** With `AGENTEVAL_AUTH_ENABLED=true` every `/api/v1` call needs a
   Bearer key, and `/health` and `/ready` stay open (they are what the container healthchecks use). Create the first
   key from inside the API container, where the database is reachable:

   ```bash
   $AEC run --rm api python -m agenteval_api.keys create --name admin --scopes read,write,admin
   # Optional: --expires-days N. `keys list` shows keys (never secrets); `keys revoke <prefix>` revokes one.
   ```

   The CLI prints the key once. Store it in a password manager; only its hash is kept. Then call the API:

   ```bash
   curl -fsS -H "Authorization: Bearer $KEY" https://$AGENTEVAL_DOMAIN/api/v1/stats/overview
   ```

7. **Create a limited key for the dashboard** (not admin) with the same CLI —
   `keys create --name dashboard --scopes read,write` (or `--scopes read` for a view-only
   dashboard) — and enter it in the dashboard's key screen; it is kept in the browser session only. Keep the admin key for key management only. Revoke keys with the CLI when someone leaves or a key leaks.

Scopes are independent (`write` does not imply `read`); see [docs/api/auth.md](api/auth.md).

## Upgrades

Migrations are applied with the workers stopped, because a worker that is running the old code
against a new schema (or the reverse) can write rows the new constraints reject.

```bash
# first set AGENTEVAL_VERSION=v1.2.3 in /etc/agenteval/prod.env
$AEC pull api worker web                   # or: $AEC build
$AEC stop worker                           # drains in-flight runs: up to 340 s (stop_grace_period)
$AEC run --rm api alembic -c packages/core/alembic.ini upgrade head
$AEC up -d --wait                          # recreates api, worker and web on the new images
```

`stop worker` waits for in-flight runs to finish (the drain), and runs that cannot finish are
cancelled and recorded by the worker itself. If the host is lost mid-run, lease expiry and the reconciler recover the work
([ADR-009](decisions/ADR-009-reliability-and-recovery.md)). Take a [backup](#backups-and-restore) before every upgrade that
includes a migration. The API restarts briefly; `caddy` keeps serving and clients get a 502 for that moment.

### Downgrades

Do not plan on `alembic downgrade`. Several migrations **refuse** to downgrade once the data
they introduced exists, rather than silently destroying it: `0002` (agent data), `0003` (Phase 4
evaluation data), `0004` (Phase 5 reliability data), `0007` (workers recorded as lost) and `0009`
(recorded replays). A refusal aborts the downgrade with a message and changes nothing. The supported way back to an older
release is **restore the pre-upgrade backup** and run the older images ([Rollback](#rollback)).

## Backups and restore

State lives in three places, and two of them must be backed up **together** ([ADR-008](decisions/ADR-008-artifact-storage-and-trajectory-fence.md)):

1. **Postgres** (source of truth for runs, attempts, evaluations, results, keys).
2. **The `artifacts` volume** (workspace snapshot files; Postgres only holds the rows). A restored database
   whose files are missing produces platform errors, not wrong grades.
3. Redis holds only queue state that the reconciler rebuilds from Postgres; it needs no backup. The Caddy `caddy_data`
   volume holds certificates; back it up if you want to avoid re-issuing (Let's Encrypt has rate limits).

Back up with the workers stopped so the pair is consistent:

```bash
STAMP=$(date +%Y%m%d-%H%M%S); DEST=/var/backups/agenteval; sudo install -d -m 700 $DEST
$AEC stop worker
$AEC exec -T postgres pg_dump -U agenteval -Fc agenteval > $DEST/db-$STAMP.dump
docker run --rm -v agenteval_artifacts:/data:ro -v $DEST:/out alpine \
  tar czf /out/artifacts-$STAMP.tar.gz -C /data .
$AEC start worker
```

The volume name is `<compose project>_artifacts` (`docker volume ls | grep artifacts`; the project
defaults to the repository directory name). Copy both files off the host. If the worker cannot be stopped, dump
Postgres first and copy the volume second: a snapshot file is written before its row, so every row in the dump has
its file in the later copy (the reverse order can leave rows without files).

Test a restore on a scratch host before you need it:

```bash
$AEC down                                                    # keeps volumes; add -v only to wipe
$AEC up -d --wait postgres redis
$AEC exec -T postgres dropdb -U agenteval agenteval && \
  $AEC exec -T postgres createdb -U agenteval agenteval
$AEC exec -T postgres pg_restore -U agenteval -d agenteval --no-owner < $DEST/db-<stamp>.dump
docker run --rm -v agenteval_artifacts:/data -v $DEST:/in alpine \
  sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /in/artifacts-<stamp>.tar.gz -C /data'
$AEC up -d --wait
```

The restored schema is at the revision of the dump: run the images that match it (see Rollback).

## Rollback

1. `$AEC stop worker api`.
2. Restore the backup taken **before** the upgrade (Postgres and artifacts together, as above).
3. Set `AGENTEVAL_VERSION` back to the old tag and `$AEC up -d --wait`. If you built on the host, check out the old release and build again.

Anything created after that backup is lost (the system of record is Postgres). If the upgrade had no
migration, no restore is needed: set the old tag and `up -d`.

## Observability in production

Metrics (Prometheus) and tracing (Jaeger) are optional. Both UIs bind to `127.0.0.1` only, so reach them through
an SSH tunnel (`ssh -L 9090:127.0.0.1:9090 -L 16686:127.0.0.1:16686 host`), not through Caddy.

```bash
$AEC -f docker-compose.observability.yml --profile observability up -d prometheus jaeger api worker
```

- Compose merges later `-f` files, so list `docker-compose.observability.yml` after the prod file as shown (with `AEC` already
  holding the first two). It sets `OTEL_EXPORTER_OTLP_ENDPOINT` on `api` and `worker`.
- Prometheus scrapes `api:8000/metrics` and every worker replica on `9464` over the internal network. The public proxy
  never serves `/metrics`.
- Jaeger keeps traces in memory: they are gone on restart. Prometheus keeps 7 days (`promdata` volume).
- Logs are JSON lines on stdout: `$AEC logs -f api worker`. Docker's default `json-file` driver does not rotate; set
  `"log-opts": {"max-size": "50m", "max-file": "5"}` in `/etc/docker/daemon.json`.

## The Docker socket

The worker mounts `/var/run/docker.sock`. Anyone who can talk to that socket can start a privileged container with the host
filesystem mounted, so **the worker container is root-equivalent on the host**. The sandbox's protections (no network,
read-only root, dropped capabilities, resource limits, a reaper) protect the host from *agent code*; they do not protect it from
a compromised worker. Consequences:

- Use a **dedicated host or VM** that holds nothing else of value, and no other credentials.
- Keep every other service off the host's public interface (the prod override publishes only Caddy) and keep authentication on.
- Do not mount the socket into any other service, and do not give agent code a path to the worker.
- A socket proxy that allows only the container calls the worker needs would reduce the exposure; it is not built here.

The production override also drops every Linux capability from the api, worker, web and Caddy containers, sets
`no-new-privileges` on them, and gives the API a read-only root filesystem (Caddy keeps only `NET_BIND_SERVICE`). The worker
still runs as root inside its container, because of the socket.

Details: the README's [security model](../README.md#12-security-model) and [docs/security-review.md](security-review.md).

## Sizing

The limits in `docker-compose.prod.yml` are starting points for a 4 vCPU / 8 GB host, not measurements:
Postgres 2 CPU / 2 GB, worker 2 CPU / 2 GB, API 1 CPU / 1 GB, dashboard 512 MB, Redis 512 MB, Caddy 256 MB. The worker's limit covers the
worker process only; **sandbox containers run outside it** (each has its own limits), so budget host CPU and memory for
`AGENTEVAL_WORKER_CONCURRENCY` sandboxes per worker on top. A cpu or memory squeeze on the worker, API or
Postgres delays lease heartbeats, which is why the base file gives them a higher CPU weight.

- Throughput and worker-scaling numbers, with their method and limits, are in [docs/benchmarks/](benchmarks/README.md). They were measured on
  a developer machine with a single uvicorn process; read the observations and limitations before extrapolating.
- Add capacity with more worker replicas: `$AEC up -d --scale worker=3` (Prometheus discovers every replica). All workers
  share the one `artifacts` volume, so replicas must run on the same host.
- `AGENTEVAL_API_WORKERS` defaults to 1. The `/metrics` endpoint reads a per-process registry, so with more than one uvicorn
  process each scrape returns one arbitrary process's counters. Raise it only if you accept that, or scrape elsewhere.
- Leave the lease settings at their defaults. The short timings in `docker-compose.e2e.yml` are for the test suite only.
