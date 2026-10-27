#!/usr/bin/env bash
# Dump a stack's database as the demo snapshot the read-only image restores
# (infrastructure/docker/api-demo.Dockerfile, docs/deployment.md "Free hosting").
#
#   scripts/make_demo_snapshot.sh [postgres-container] [output]
#
# Defaults: the local compose stack's agenteval-postgres-1 and
# infrastructure/demo/agenteval-demo.sql.gz. The dump is the full schema plus data, with:
#   - no rows of api_keys (the table itself stays, empty),
#   - no owners or grants,
# and it is refused if anything in it looks like a secret or a host path: an AgentEval key,
# a provider key, a private key, a home directory or the Docker socket. Trajectories only
# ever hold sandbox paths (/workspace/...), so a hit means something unexpected leaked in.
#
# See docs/deployment.md for the whole procedure (fresh stack, scripts/demo_seed.py, this).
set -euo pipefail

CONTAINER="${1:-agenteval-postgres-1}"
OUT="${2:-infrastructure/demo/agenteval-demo.sql.gz}"
DB_USER="${AGENTEVAL_DB_USER:-agenteval}"
DB_NAME="${AGENTEVAL_DB_NAME:-agenteval}"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
  --no-owner --no-privileges --exclude-table-data=api_keys >"$tmp"

# The data section of api_keys must be absent; the CREATE TABLE stays.
if grep -q '^COPY public.api_keys ' "$tmp"; then
  echo "make_demo_snapshot: api_keys rows are in the dump" >&2
  exit 1
fi
PATTERNS=(
  'ae_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}'   # an AgentEval API key
  'sk-ant-[A-Za-z0-9_-]+'                # an Anthropic key
  'ANTHROPIC_API_KEY *='
  'BEGIN [A-Z ]*PRIVATE KEY'
  '/home/[A-Za-z0-9_.-]+'
  '/Users/[A-Za-z0-9_.-]+'
  'docker\.sock'
)
for pattern in "${PATTERNS[@]}"; do
  if grep -qE -- "$pattern" "$tmp"; then
    echo "make_demo_snapshot: the dump matches /$pattern/:" >&2
    grep -noE -- "$pattern" "$tmp" | head -5 >&2
    exit 1
  fi
done

mkdir -p "$(dirname "$OUT")"
gzip -9 -n <"$tmp" >"$OUT"
tables=$(grep -c '^COPY public\.' "$tmp")
echo "wrote $OUT ($(wc -c <"$OUT") bytes gzipped, $(wc -c <"$tmp") bytes of SQL, $tables tables with data)"
