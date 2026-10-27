#!/usr/bin/env bash
# The read-only demo container (infrastructure/docker/api-demo.Dockerfile): a throwaway
# PostgreSQL 16 and Redis inside the container, the demo snapshot restored into them, then
# the API on $PORT. Everything lives under a fresh temporary directory, so every start is
# the same snapshot again and nothing survives a restart. Sized for a 512 MiB instance.
set -euo pipefail

PGBIN=/usr/lib/postgresql/16/bin
DEMO_DIR=/app/demo
RUN_DIR="$(mktemp -d /tmp/agenteval-demo.XXXXXX)"
PGDATA="$RUN_DIR/pg"
started=$(date +%s)
log() { printf '{"event": "demo_%s", "elapsed_s": %d}\n' "$1" "$(($(date +%s) - started))"; }

"$PGBIN/initdb" -D "$PGDATA" -U agenteval --auth=trust --encoding=UTF8 --no-locale \
  --no-instructions >/dev/null
cat >>"$PGDATA/postgresql.conf" <<'CONF'
# demo: loopback only, a handful of connections, small buffers, nothing durable
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = ''
max_connections = 10
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
"$PGBIN/pg_ctl" -D "$PGDATA" -l "$RUN_DIR/postgres.log" -w -t 120 start >/dev/null
log postgres_started

redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no \
  --maxmemory 16mb --maxmemory-policy allkeys-lru \
  --dir "$RUN_DIR" --logfile "$RUN_DIR/redis.log" --daemonize yes
log redis_started

psql() { "$PGBIN/psql" -X -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U agenteval "$@"; }
"$PGBIN/createdb" -h 127.0.0.1 -U agenteval agenteval
gunzip -c "$DEMO_DIR/agenteval-demo.sql.gz" | psql -d agenteval >/dev/null
psql -d agenteval -f "$DEMO_DIR/refresh-timestamps.sql"
psql -d agenteval -c 'ANALYZE'
log snapshot_restored

# One worker process; proxy headers so the per-client rate limit sees the visitor's
# address behind the platform's proxy rather than the proxy's.
exec uvicorn agenteval_api.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
  --proxy-headers --forwarded-allow-ips '*'
