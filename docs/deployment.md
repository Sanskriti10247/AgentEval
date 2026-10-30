# Deployment

How to run AgentEval on a single dedicated Linux host with Docker Compose (ADR-012 F). This is a research and portfolio deployment, not a hardened multi-tenant platform: read [the Docker socket](#the-docker-socket) before you expose it.

For a public demo that costs nothing, see [Free hosting (Vercel + Render)](#free-hosting-vercel--render) at the end.

What runs:

| Service | Published | Notes |
|---|---|---|
| `caddy` | 80, 443 | TLS (automatic certificates for `AGENTEVAL_DOMAIN`), proxies `/api/*`, `/health`, `/ready` to the API and everything else to the dashboard. `/metrics` is answered 404. |
| `api` | no | uvicorn, no reload |
| `web` | no | Next.js standalone server |
| `worker` | no | `python -m agenteval_worker.main`; holds the Docker socket and the artifact volume |
| `postgres`, `redis` | no | passwords required; data in named volumes |
| `prometheus`, `jaeger` | loopback only | optional, `--profile observability` |

Sandbox containers are not compose services: the worker creates them on demand, as siblings, through the Docker socket.

## Prerequisites

- A Linux host dedicated to AgentEval (a single VPS is enough; see [the Docker socket](#the-docker-socket) for why dedicated), with Docker Engine and Compose v2. Compose must be 2.24 or newer, because the override file uses `!reset` and `!override`; check with `docker compose version`. Install both from [Docker's packages](https://docs.docker.com/engine/install/) rather than a distribution's older ones.
- A DNS name pointing at the host, and ports 80 and 443 reachable from the internet (Caddy answers the ACME challenge itself). Nothing else should be published. A host firewall should allow only SSH, 80 and 443 inbound. Ports that Docker publishes bypass `ufw`, which is one reason the prod override publishes nothing but Caddy.
- The repository checked out on the host at the release you deploy: the compose files, `infrastructure/caddy/Caddyfile`, and the Dockerfiles if you build locally. Keep the checkout and `AGENTEVAL_VERSION` on the same tag:

  ```bash
  git clone https://github.com/Sanskriti10247/AgentEval.git /opt/agenteval
  cd /opt/agenteval && git checkout v1.2.3
  ```

- Enough capacity; see [Sizing](#sizing).

## First deploy

Run every command from the repository root in a root shell (`sudo -i`). The env file below is readable by root only, and access to the Docker daemon is root-equivalent anyway. In that shell, define a shorthand for the two compose files and the env file:

```bash
export AEC='docker compose --env-file /etc/agenteval/prod.env -f docker-compose.yml -f docker-compose.prod.yml'
```

1. Create the env file outside the repository.

   ```bash
   sudo install -d -m 700 /etc/agenteval
   sudo install -m 600 .env.prod.example /etc/agenteval/prod.env
   sudoedit /etc/agenteval/prod.env
   ```

   Set `AGENTEVAL_DOMAIN`, generate `POSTGRES_PASSWORD` and `REDIS_PASSWORD` (letters and digits, for example `openssl rand -base64 36 | tr -d '/+=' | cut -c1-40`), and pin `AGENTEVAL_VERSION` to a release tag. Never commit this file. `make prod-config` checks that the compose files parse with throwaway secrets; `$AEC config -q` checks them with your real file.

   | Variable | Required | Meaning |
   |---|---|---|
   | `AGENTEVAL_DOMAIN` | yes | Public hostname; Caddy gets its certificate, CORS allows `https://<domain>` |
   | `POSTGRES_PASSWORD`, `REDIS_PASSWORD` | yes | Internal data-store passwords (letters and digits: they go into URLs) |
   | `AGENTEVAL_IMAGE_PREFIX`, `AGENTEVAL_VERSION` | no | Image source and tag; defaults `ghcr.io/sanskriti10247/agenteval` and `latest`, so always pin the tag |
   | `AGENTEVAL_API_WORKERS` | no | uvicorn processes (default 1, see [Sizing](#sizing)) |
   | `AGENTEVAL_WORKER_CONCURRENCY`, `AGENTEVAL_LOG_LEVEL` | no | Runs per worker (default 2), log level (default `INFO`) |
   | `AGENTEVAL_AUTH_ENABLED` | no | Default `true`; keep it on |
   | `AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER`, `ANTHROPIC_API_KEY` | no | Real model calls (off by default; the key reaches the worker only) |
   | `AGENTEVAL_REAL_EVAL_MAX_COST_USD` | no | Spending cap in USD stored on a real-model evaluation whose request names no `max_cost_usd` (default `2.00`, see [Real model spend](#real-model-spend)) |
   | `AGENTEVAL_REAL_PROVIDER_CONCURRENCY` | no | Real-model agent runs one worker process executes at once (default 1) |

   Compose reads these for substitution only. Other settings in `.env.example` keep their defaults unless you add them to a service's `environment` yourself. With `--env-file`, a `.env` in the repository is not read.

2. Get the images, in one of two ways.

   - Pull the release images, which `release.yml` publishes on a `v*` tag: `$AEC pull api worker web`. The published `web` image bakes in the API address it was released with (repository variable `AGENTEVAL_WEB_API_URL`, default `http://localhost:8000`), so it only works at that address. For any other domain, build `web` on the host instead.
   - Build on the host: `$AEC build`. This builds `api`, `worker` and `web`, with `https://$AGENTEVAL_DOMAIN` baked into the dashboard.

3. Provide the sandbox image under the allow-listed name. The worker only runs images in `AGENTEVAL_IMAGE_ALLOWLIST` (default `agenteval-sandbox:py312`), and the image must exist on the host's Docker daemon:

   ```bash
   docker build -t agenteval-sandbox:py312 infrastructure/docker/sandbox          # or:
   docker pull ghcr.io/<owner>/agenteval-sandbox:<tag> && \
     docker tag ghcr.io/<owner>/agenteval-sandbox:<tag> agenteval-sandbox:py312
   ```

4. Start the data stores and migrate, with the workers stopped.

   ```bash
   $AEC up -d --wait postgres redis
   $AEC run --rm api alembic -c packages/core/alembic.ini upgrade head
   ```

   If `api` or `worker` starts before this step, it fails because the schema is missing. That is expected.

5. Start everything.

   ```bash
   $AEC up -d --wait
   $AEC ps
   curl -fsS https://agenteval.example.com/ready     # your AGENTEVAL_DOMAIN
   ```

   The first request triggers certificate issuance and can take a few seconds. If it fails, read `$AEC logs caddy`; the usual causes are DNS not pointing at the host, or port 80 being blocked.

6. Bootstrap an admin key. With `AGENTEVAL_AUTH_ENABLED=true`, every `/api/v1` call needs a Bearer key, while `/health` and `/ready` stay open because the container healthchecks use them. Create the first key from inside the API container, where the database is reachable:

   ```bash
   $AEC run --rm api python -m agenteval_api.keys create --name admin --scopes read,write,admin
   # Optional: --expires-days N. `keys list` shows keys (never secrets); `keys revoke <prefix>` revokes one.
   ```

   The CLI prints the key once. Store it in a password manager; only its hash is kept. Then call the API:

   ```bash
   curl -fsS -H "Authorization: Bearer $KEY" https://$AGENTEVAL_DOMAIN/api/v1/stats/overview
   ```

7. Create a separate, non-admin key for the dashboard with the same CLI, `keys create --name dashboard --scopes read,write` (or `--scopes read` for a view-only dashboard), and enter it on the dashboard's key screen; the browser keeps it for the session only. Use the admin key for key management and nothing else, and revoke keys with the CLI when someone leaves or a key leaks.

Scopes are independent: `write` does not imply `read`. See [api/auth.md](api/auth.md).

## Upgrades

Migrations are applied with the workers stopped, because a worker running old code against a new schema, or the reverse, can write rows the new constraints reject.

```bash
git fetch --tags && git checkout v1.2.3    # compose files and Caddyfile of the new release
# then set AGENTEVAL_VERSION=v1.2.3 in /etc/agenteval/prod.env
$AEC pull api worker web                   # or: $AEC build
$AEC stop worker                           # drains in-flight runs: up to 340 s (stop_grace_period)
$AEC run --rm api alembic -c packages/core/alembic.ini upgrade head
$AEC up -d --wait                          # recreates api, worker and web on the new images
```

The data stores track their major version (`postgres:16`, `redis:7`; Caddy is pinned exactly), so `$AEC pull postgres redis` before `up` picks up their patch releases. A Postgres major upgrade is a dump and restore, not a tag change.

`stop worker` waits for in-flight runs to finish, and the worker itself cancels and records any run that cannot finish in time. If the host is lost mid-run, lease expiry and the reconciler recover the work ([ADR-009](decisions/ADR-009-reliability-and-recovery.md)). Take a [backup](#backups-and-restore) before every upgrade that includes a migration. The API restarts briefly; `caddy` keeps serving, and clients get a 502 for that moment.

### Downgrades

Do not plan on `alembic downgrade`. Several migrations refuse to downgrade once the data they introduced exists, rather than silently destroying it: `0002` (agent data), `0003` (Phase 4 evaluation data), `0004` (Phase 5 reliability data), `0007` (workers recorded as lost) and `0009` (recorded replays). A refusal aborts the downgrade with a message and changes nothing. The supported way back to an older release is to restore the pre-upgrade backup and run the older images ([Rollback](#rollback)).

## Backups and restore

State lives in three places, and the first two must be backed up together ([ADR-008](decisions/ADR-008-artifact-storage-and-trajectory-fence.md)):

1. Postgres, the source of truth for runs, attempts, evaluations, results and keys.
2. The `artifacts` volume, which holds the workspace snapshot files; Postgres only holds their rows. A restored database whose files are missing produces platform errors, not wrong grades.
3. Redis, which holds only queue state that the reconciler rebuilds from Postgres, so it needs no backup. The Caddy `caddy_data` volume holds certificates; back it up if you want to avoid re-issuing them, since Let's Encrypt has rate limits.

Back up with the workers stopped, so the pair is consistent (as root, like every `$AEC` command):

```bash
STAMP=$(date +%Y%m%d-%H%M%S); DEST=/var/backups/agenteval; sudo install -d -m 700 $DEST
$AEC stop worker
$AEC exec -T postgres pg_dump -U agenteval -Fc agenteval > $DEST/db-$STAMP.dump
docker run --rm -v agenteval_artifacts:/data:ro -v $DEST:/out alpine:3 \
  tar czf /out/artifacts-$STAMP.tar.gz -C /data .
$AEC start worker
```

The volume name is `<compose project>_artifacts` (`docker volume ls | grep artifacts`); the project name defaults to the repository directory name, lower-cased. Copy both files off the host, and schedule the same commands (for example, a nightly root cron job or systemd timer running a script with the lines above) rather than relying on memory. If the worker cannot be stopped, dump Postgres first and copy the volume second. A snapshot file is written before its row, so every row in the dump has its file in the later copy; the reverse order can leave rows without files.

Test a restore on a scratch host before you need it:

```bash
$AEC down                                                    # keeps volumes; add -v only to wipe
$AEC up -d --wait postgres redis
$AEC exec -T postgres dropdb -U agenteval agenteval && \
  $AEC exec -T postgres createdb -U agenteval agenteval
$AEC exec -T postgres pg_restore -U agenteval -d agenteval --no-owner < $DEST/db-<stamp>.dump
docker run --rm -v agenteval_artifacts:/data -v $DEST:/in alpine:3 \
  sh -c 'rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /in/artifacts-<stamp>.tar.gz -C /data'
$AEC up -d --wait
```

The restored schema is at the revision of the dump, so run the images that match it (see [Rollback](#rollback)).

## Rollback

1. `$AEC stop worker api`.
2. Restore the backup taken before the upgrade, Postgres and artifacts together, as above.
3. Set `AGENTEVAL_VERSION` back to the old tag and run `$AEC up -d --wait`. If you built on the host, check out the old release and build again.

Anything created after that backup is lost, since Postgres is the system of record. If the upgrade had no migration, no restore is needed: set the old tag and run `up -d`.

## Observability in production

Metrics (Prometheus) and tracing (Jaeger) are optional. Both UIs bind to `127.0.0.1` only, so reach them through an SSH tunnel (`ssh -L 9090:127.0.0.1:9090 -L 16686:127.0.0.1:16686 host`), not through Caddy.

```bash
$AEC -f docker-compose.observability.yml --profile observability up -d prometheus jaeger api worker
```

Compose merges later `-f` files, so `docker-compose.observability.yml` goes after the prod file, as above (`AEC` already holds the first two). It sets `OTEL_EXPORTER_OTLP_ENDPOINT` on `api` and `worker`. Prometheus scrapes `api:8000/metrics` and every worker replica on port 9464 over the internal network; the public proxy never serves `/metrics`. Jaeger keeps traces in memory, so they are gone on restart, while Prometheus keeps 7 days in the `promdata` volume. What is exported is described in [observability.md](observability.md).

Logs are JSON lines on stdout: `$AEC logs -f api worker`. Docker's default `json-file` driver does not rotate logs, so the prod override caps every service at 5 files of 50 MB. Sandbox containers are short-lived and removed after each attempt. For anything else on the host, set `"log-opts": {"max-size": "50m", "max-file": "5"}` in `/etc/docker/daemon.json`.

## The Docker socket

The worker mounts `/var/run/docker.sock`. Anyone who can talk to that socket can start a privileged container with the host filesystem mounted, so the worker container is root-equivalent on the host. The sandbox's protections (no network, a read-only root, dropped capabilities, resource limits, the reaper) protect the host from agent code; they do not protect it from a compromised worker. So:

- Use a dedicated host or VM that holds nothing else of value and no other credentials.
- Keep every other service off the host's public interface (the prod override publishes only Caddy), and keep authentication on.
- Do not mount the socket into any other service, and do not give agent code a path to the worker.
- A socket proxy that allows only the container calls the worker needs would reduce the exposure. It is not built here.

The production override also drops every Linux capability from the api, worker, web and Caddy containers (Caddy keeps only `NET_BIND_SERVICE`), sets `no-new-privileges` on them, and gives the API a read-only root filesystem. The worker still runs as root inside its container, because of the socket.

More in the README's [security model](../README.md#security-model) and the [security review](security-review.md).

## Sizing

The limits in `docker-compose.prod.yml` are starting points for a 4 vCPU / 8 GB host, not measurements: Postgres 2 CPU / 2 GB, worker 2 CPU / 2 GB, API 1 CPU / 1 GB, dashboard 512 MB, Redis 512 MB, Caddy 256 MB. The worker's limit covers the worker process only. Sandbox containers run outside it, each with its own limits, so budget host CPU and memory for `AGENTEVAL_WORKER_CONCURRENCY` sandboxes per worker on top. A CPU or memory squeeze on the worker, API or Postgres delays lease heartbeats, which is why the base file gives them a higher CPU weight.

- Throughput and worker-scaling numbers, with their method and limits, are in the [benchmark report](benchmarks/README.md). They were measured on a developer machine with a single uvicorn process, so read its observations and limitations before extrapolating.
- Add capacity with more worker replicas: `$AEC up -d --scale worker=3` (Prometheus discovers every replica). All workers share the one `artifacts` volume, so the replicas must run on the same host.
- `AGENTEVAL_API_WORKERS` defaults to 1. `/metrics` reads a per-process registry, so with more than one uvicorn process each scrape returns one arbitrary process's counters. Raise it only if you accept that, or scrape elsewhere.
- Leave the lease settings at their defaults. The short timings in `docker-compose.e2e.yml` are for the test suite only.

## Real model spend

Real model calls are off unless `AGENTEVAL_ENABLE_ANTHROPIC_PROVIDER=true`, and three limits bound what an evaluation can spend once they are on:

- Each agent version's `limits.max_cost_usd` caps a single attempt; the runtime checks it before every model call, so one attempt can overshoot it by at most one call.
- Each evaluation of a real-model version stores a `max_cost_usd` (the request's, or `AGENTEVAL_REAL_EVAL_MAX_COST_USD`, default 2.00). Before a run of that evaluation starts its agent loop, the worker adds up `cost_usd` over every attempt of the evaluation's runs. Once that sum reaches the cap, the run fails with `evaluation_budget_exhausted` without calling the model. Runs already in flight are not stopped, so the total can exceed the cap by what those runs spend, each bounded by its version's per-attempt cap. The failure is a platform failure: it is not retried and does not count against the pass rate.
- `AGENTEVAL_REAL_PROVIDER_CONCURRENCY` (default 1) is a semaphore inside each worker process around real-model agent runs. It is per process, not global: three worker replicas with the default run up to three real-model runs at once. A run waiting for the semaphore holds its lease (the keeper keeps renewing it) and one of the process's `AGENTEVAL_WORKER_CONCURRENCY` slots. Fake-model runs never wait for it.

`POST /api/v1/evaluations/estimate` returns the upper bounds (model requests and cost) for a planned evaluation before anything is queued; see [the evaluations API](api/evaluations.md).

## Free hosting (Vercel + Render)

A small public demo on free tiers: the dashboard on Vercel, and the API, one worker, PostgreSQL and Redis in a single Render web service (512 MiB, 0.1 CPU, no persistent disk, no Docker daemon). Visitors need no key. They can browse the sample data, launch an evaluation of an existing agent version on the frozen dataset, start a single run, watch it run, read its trajectory and grades, cancel, and replay a failed run. It is a demo of the platform, not a deployment of it:

- Only what is already there can run. The image sets `AGENTEVAL_PUBLIC_DEMO=true`: the API answers 403 to every write except launching evaluations and runs, cancel and replay, so a visitor cannot create an agent, a version (and so a model script), a task or a dataset. Evaluations take at most 3 repetitions, and at most 20 runs may be queued or running across all visitors (429 beyond that). Writes are limited to 30 a minute per visitor. [api/auth.md](api/auth.md) has the exact rules; the dashboard reads them from `GET /health`.
- No sandbox isolation. With no Docker daemon, the worker uses the local backend (`AGENTEVAL_SANDBOX_BACKEND=local`, `apps/worker/agenteval_worker/sandbox/local.py`): every command runs as a subprocess of the worker, as the same user, in a per-run directory with a scrubbed environment, rlimits (address space, CPU time, file size, process count), timeouts that kill the process group, and a reaper. That is hygiene, not a security boundary: the commands could read the container's files and reach its Postgres and Redis. It is acceptable here only because public demo mode restricts runs to the seeded agents (fixed scripts for the offline `fake` model) and the seeded tasks. Never combine the local backend with an API that accepts new agents or tasks.
- The task's image is not used. Commands run on the container's own Python 3.12 (`/usr/local/bin/python`, the same version as the sandbox image) and coreutils, and attempts record `sandbox_image_id` as `local`. The seeded agents get the same results as in Docker: rerunning all eight once gave exactly the snapshot's failure categories per repetition.
- Nothing persists. Every start copies the same snapshot into a fresh temporary directory, and Render's free instance sleeps after 15 minutes without traffic, so whatever visitors launched is gone at the next start. Postgres runs with `fsync` off.
- One worker, one run at a time. At 0.1 CPU an evaluation of the six tasks takes about half a minute; runs launched meanwhile wait in the queue.

At startup every timestamp in the snapshot is moved by the same interval so the newest is ten minutes old (`infrastructure/demo/refresh-timestamps.sql`). Without that, the overview's 24-hour to 30-day windows would be empty once the snapshot is a month old. Durations and ordering are unchanged.

The snapshot is about 57 KB gzipped. It holds one frozen dataset of six tasks (`calc-bench` v1) and eight scripted agents evaluated on it with the offline `fake` provider: 8 evaluations, 84 runs, 618 trajectory events. Between them the agents produce every failure category the comparison page shows (passed, tests failed, wrong answer, missing output, tampering, max steps) and a platform failure. The `api_keys` table is empty. Workspace snapshot files are not included, so the seeded attempts cannot be graded again; new runs save theirs under the run directory.

### What it uses

`infrastructure/docker/api-demo.Dockerfile` adds PostgreSQL 16 (from the PostgreSQL project's Debian repository), `redis-server` and `tini` to one Python environment holding the API and the worker (without the Anthropic SDK). While the image is built, `entrypoint.sh --prepare` creates the database cluster and restores the snapshot into `/app/demo/pgdata`. At start, `infrastructure/demo/entrypoint.sh` runs as the unprivileged `app` user:

1. copies that cluster to a fresh directory under `/tmp` and starts Postgres on `127.0.0.1` (`shared_buffers=16MB`, `max_connections=12`, no autovacuum), then shifts the timestamps;
2. starts Redis on `127.0.0.1` with no persistence, `maxmemory 32mb` and `noeviction`, so a queued run's stream entry is never evicted (a full Redis makes launches fail with 503 instead);
3. starts one uvicorn process on `$PORT`, and once it listens, the worker (`python -m agenteval_worker.main`) with concurrency 1, no metrics port, artifacts and run directories under the same temporary directory, and the lease keeper on a thread (`AGENTEVAL_LEASE_KEEPER=thread`) rather than in a second interpreter, which saves about 100 MiB;
4. waits for either process. If the API or the worker exits, the other is stopped and the container exits, so Render restarts it. On SIGTERM both stop, the worker drains for up to 20 seconds, then Postgres and Redis stop.

The API's pool is 3 connections plus 2 overflow, the worker's 2 plus 1. uvicorn trusts `X-Forwarded-For`, so the per-client budgets apply to each visitor rather than to Render's proxy. A visitor who forges the header only escapes those budgets, not the global cap on queued runs.

Measured locally with `docker run --memory 512m --memory-swap 512m --cpus 0.1 -e PORT=10000`, then over HTTP: the evaluation of `careful-fixer` v2 on `calc-bench` with one repetition, a single run, a cancelled 18-run evaluation, a cancelled running run, a failed run and its replay, and the rejected writes:

| | |
|---|---|
| Container start to passing `/ready` | 20 to 28 s; the worker registers 10 to 16 s later |
| Evaluation of `careful-fixer` v2, 6 runs, 5 graded, once the worker was up | 27 to 40 s; 4 passed, 1 tests failed, 1 ungraded, as in the snapshot |
| Evaluation of `sloppy-patcher` v1 (shorter scripts) | 11 s |
| Single run, launch to `COMPLETED` | 3 to 8 s, graded a moment later |
| Cancel of a running run to `CANCELLED` | 3 to 5 s |
| cgroup `memory.peak` (includes page cache) | 272 to 312 MiB |
| anonymous memory after the evaluations | 169 to 172 MiB |
| uvicorn / worker resident | about 100 / 95 MiB |
| Postgres postmaster / each backend, resident | 19 / 23 to 28 MiB (each counts the shared buffers) |
| Redis, resident | 8 MiB |
| `docker stop` | 5 s |

With the lease keeper in its own process instead, that process alone was 88 MiB resident plus 15 MiB for multiprocessing's resource tracker. Before the snapshot was restored at build time (`--prepare`), `initdb` and the restore alone took about 20 seconds at 0.1 CPU. A request that wakes a sleeping instance waits for Render to start the container plus the 20 to 30 seconds above, and the dashboard shows "Connecting to the API" meanwhile.

### Render (API)

New, Web Service, connect the repository, then:

| Setting | Value |
|---|---|
| Language | Docker |
| Branch | `main` |
| Root Directory | empty (the repository root) |
| Dockerfile Path | `infrastructure/docker/api-demo.Dockerfile` |
| Docker Build Context Directory | `.` |
| Docker Command | empty (the image's entrypoint) |
| Instance Type | Free |
| Health Check Path | `/ready` |
| Environment | `AGENTEVAL_CORS_ORIGINS=["https://<project>.vercel.app"]` |

Render sets `PORT` (10000) itself, and the image already sets `AGENTEVAL_PUBLIC_DEMO=true`, the local sandbox backend and the database, Redis, pool and worker settings, so the CORS origin is the only variable to add. To serve the data without running anything, add `AGENTEVAL_READ_ONLY=true`: every write then answers 403 and the worker idles. It is a JSON list; add a custom domain or a preview URL to it if you use one. `make demo-image` builds the same image locally.

### Vercel (dashboard)

Add New, Project, import the repository, then:

| Setting | Value |
|---|---|
| Root Directory | `apps/web` |
| Framework Preset | Next.js (detected) |
| Build and install commands | defaults (`next build`, `npm install` from `package-lock.json`) |
| Environment variable | `NEXT_PUBLIC_API_URL=https://<service>.onrender.com` |

`NEXT_PUBLIC_API_URL` is inlined at build time, so redeploy after changing it. Nothing else is needed: the dashboard calls the API from the browser and reads no files or Docker paths. Preview deployments get their own `*.vercel.app` origins, which the API rejects unless you add them to `AGENTEVAL_CORS_ORIGINS`.

### Regenerating the snapshot

Do this after any migration (the image applies none, and `packages/core/tests/test_demo_snapshot.py` fails when the snapshot is behind the head) or to change the data. Use a fresh stack under its own project name and ports, so the local one is untouched:

```bash
make sandbox-image
export COMPOSE_PROJECT_NAME=agenteval-demo-seed AGENTEVAL_PG_HOST_PORT=18232 \
  AGENTEVAL_REDIS_HOST_PORT=18233 AGENTEVAL_API_HOST_PORT=18280 AGENTEVAL_WORKER_CONCURRENCY=4
docker compose up -d --build --wait postgres redis api worker
docker compose run --rm api alembic -c packages/core/alembic.ini upgrade head
docker compose restart worker
docker compose exec -T api python -m agenteval_api.keys create --name seed --scopes read,write
AGENTEVAL_API_URL=http://127.0.0.1:18280 AGENTEVAL_API_KEY=ae_... uv run python scripts/demo_seed.py
docker compose stop worker                                       # records the worker as stopped
scripts/make_demo_snapshot.sh agenteval-demo-seed-postgres-1     # writes infrastructure/demo/agenteval-demo.sql.gz
docker compose down -v
```

`scripts/demo_seed.py` takes a few minutes and prints each evaluation's summary and a comparison link. `scripts/make_demo_snapshot.sh` dumps the schema and data without owners or grants, leaves out the rows of `api_keys`, and refuses to write the file if the dump contains anything shaped like an AgentEval or Anthropic key, a private key, a home directory or the Docker socket path. Trajectories only ever hold sandbox paths (`/workspace/...`). Read the diff of the dump before committing it all the same.

