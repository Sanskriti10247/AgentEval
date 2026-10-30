#!/usr/bin/env bash
# The demo container (infrastructure/docker/api-demo.Dockerfile): a throwaway PostgreSQL 16
# and Redis inside the container, the demo snapshot restored into them, then one worker in
# the background and the API on $PORT. Everything lives under a fresh temporary directory,
# so every start is the same snapshot again and nothing survives a restart. Sized for a
# 512 MiB instance.
#
# If the API or the worker exits, the other is stopped and the container exits with that
# status (the platform restarts it). SIGTERM/SIGINT from tini stop both the same way: the
# worker drains for AGENTEVAL_WORKER_DRAIN_TIMEOUT_S, then Postgres and Redis go down.
#
# `entrypoint.sh --prepare`, run once while the image is built, creates the cluster and
# restores the snapshot into /app/demo/pgdata. A start then only copies that directory:
# initdb and the restore cost most of the startup at 0.1 CPU. Without it, a start does both.
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
DEMO_DIR=/app/demo
TEMPLATE="$DEMO_DIR/pgdata"
started=$(date +%s)
log() { printf '{"event": "demo_%s", "elapsed_s": %d}\n' "$1" "$(($(date +%s) - started))"; }
psql() { "$PGBIN/psql" -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U agenteval "$@"; }

create_cluster() {  # $1: data directory, $2: log file
  "$PGBIN/initdb" -D "$1" -U agenteval --auth=trust --encoding=UTF8 --no-locale \
    --no-instructions >/dev/null
  cat >>"$1/postgresql.conf" <<'CONF'
# demo: loopback only, a handful of connections, small buffers, nothing durable
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = ''
max_connections = 12
shared_buffers = 16MB
work_mem = 2MB
maintenance_work_mem = 16MB
temp_buffers = 2MB
max_worker_processes = 2
max_parallel_workers = 0
max_parallel_workers_per_gather = 0
jit = off
fsync = off
synchronous_commit = off
full_page_writes = off
wal_level = minimal
max_wal_senders = 0
min_wal_size = 32MB
max_wal_size = 64MB
checkpoint_timeout = 30min
autovacuum = off
log_min_messages = warning
CONF
  "$PGBIN/pg_ctl" -D "$1" -l "$2" -w -t 120 start >/dev/null
  "$PGBIN/createdb" -h 127.0.0.1 -U agenteval agenteval
  gunzip -c "$DEMO_DIR/agenteval-demo.sql.gz" | psql -d agenteval >/dev/null
  psql -d agenteval -c 'ANALYZE'
}

if [ "${1:-}" = "--prepare" ]; then
  create_cluster "$TEMPLATE" /tmp/agenteval-prepare.log
  "$PGBIN/pg_ctl" -D "$TEMPLATE" -m fast -w stop >/dev/null  # clean: no recovery at start
  rm -f /tmp/agenteval-prepare.log "$TEMPLATE/postmaster.opts"
  log prepared
  exit 0
fi

RUN_DIR="$(mktemp -d /tmp/agenteval-demo.XXXXXX)"
PGDATA="$RUN_DIR/pg"
if [ -f "$TEMPLATE/PG_VERSION" ]; then
  cp -a "$TEMPLATE" "$PGDATA"
  "$PGBIN/pg_ctl" -D "$PGDATA" -l "$RUN_DIR/postgres.log" -w -t 120 start >/dev/null
else
  create_cluster "$PGDATA" "$RUN_DIR/postgres.log"
fi
psql -d agenteval -f "$DEMO_DIR/refresh-timestamps.sql"
log snapshot_restored

# noeviction: the run and evaluation streams live here, and an evicted entry would be a run
# nobody executes. A full Redis makes the API answer 503 instead (nothing is lost: Postgres
# holds every run, and the reconciler re-dispatches).
redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no \
  --maxmemory 32mb --maxmemory-policy noeviction \
  --dir "$RUN_DIR" --logfile "$RUN_DIR/redis.log" --daemonize yes
log redis_started

# Snapshots and the local sandbox's run directories, on the same throwaway disk.
export AGENTEVAL_ARTIFACT_DIR="$RUN_DIR/artifacts"
export AGENTEVAL_LOCAL_SANDBOX_DIR="$RUN_DIR"

# One API process; proxy headers so the per-client rate limit sees the visitor's address
# behind the platform's proxy rather than the proxy's.
port="${PORT:-8000}"
uvicorn agenteval_api.main:app --host 0.0.0.0 --port "$port" --workers 1 \
  --proxy-headers --forwarded-allow-ips '*' &
api=$!

# The worker starts once the API listens (uvicorn binds after its startup): at 0.1 CPU the
# two imports would share the CPU and hold back the first response. Runs launched in the
# meantime just wait in the queue.
for _ in $(seq 1 600); do
  (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && break
  kill -0 "$api" 2>/dev/null || break
  sleep 0.5
done
log api_listening

# The worker gets a smaller connection pool than the API: concurrency is 1.
AGENTEVAL_DB_POOL_SIZE=2 AGENTEVAL_DB_MAX_OVERFLOW=1 python -m agenteval_worker.main &
worker=$!

stop() { kill -TERM "$api" "$worker" 2>/dev/null || true; }
trap stop TERM INT

set +e
wait -n "$api" "$worker"
status=$?
stop
wait "$api" "$worker" 2>/dev/null
"$PGBIN/pg_ctl" -D "$PGDATA" -m fast -t 10 stop >/dev/null 2>&1
redis-cli -h 127.0.0.1 -p 6379 shutdown nosave >/dev/null 2>&1
log stopped
exit "$status"
