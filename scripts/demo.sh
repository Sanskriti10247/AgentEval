#!/usr/bin/env bash
# A scripted walkthrough of AgentEval's core loop against a running stack:
#   create agents -> create a dataset of tasks with hidden checks -> freeze it
#   -> evaluate every agent on it -> read summaries -> compare.
#
# It uses the deterministic, offline, free `fake` model provider: each "agent" is a script of
# model turns, so the numbers below are the same on every run and nothing is billed.
#
# Needs: curl, jq, a stack (`make up && make migrate`), and an API key with read+write scopes:
#   docker compose exec -T api python -m agenteval_api.keys create --name demo --scopes read,write,admin
#   export AGENTEVAL_API_KEY=ae_...
# Optional: AGENTEVAL_API_URL (default http://localhost:8000), AGENTEVAL_DASHBOARD_URL
# (default http://localhost:3000, only used in the closing hint).
#
# Re-running is safe: names and idempotency keys carry a per-run suffix.
set -euo pipefail

API_URL="${AGENTEVAL_API_URL:-http://localhost:8000}"
DASH_URL="${AGENTEVAL_DASHBOARD_URL:-http://localhost:3000}"
IMAGE="agenteval-sandbox:py312"
API="$API_URL/api/v1"
RUN_ID="$(date +%s)-$RANDOM"

for tool in curl jq; do
  command -v "$tool" >/dev/null || { echo "demo.sh: $tool is required" >&2; exit 2; }
done
if [ -z "${AGENTEVAL_API_KEY:-}" ]; then
  echo "demo.sh: set AGENTEVAL_API_KEY (see the header of this script)" >&2
  exit 2
fi
curl -sf "$API_URL/ready" >/dev/null || { echo "demo.sh: the API at $API_URL is not ready (make up && make migrate)" >&2; exit 2; }

AUTH="Authorization: Bearer $AGENTEVAL_API_KEY"
post() { curl -sf -X POST "$API$1" -H "$AUTH" -H 'content-type: application/json' "${@:2}"; }
get() { curl -sf -H "$AUTH" "$API$1"; }
step() { printf '\n== %s\n' "$*"; }

# -- 1. agents ------------------------------------------------------------------------------
step "1. Three scripted agents"
version() {  # $1 agent name, $2 tools (JSON), $3 script (JSON) -> prints the agent version id
  local agent
  agent=$(post /agents -d "{\"name\": \"$1-$RUN_ID\"}" | jq -r .id)
  post "/agents/$agent/versions" -d "{\"model_provider\": \"fake\", \"model\": \"fake-model\",
    \"system_prompt\": \"You fix failing tests.\",
    \"tools\": $2, \"limits\": {\"max_steps\": 10}, \"model_settings\": {\"script\": $3}}" | jq -r .id
}
FIXER=$(version fixer '["write_file", "run_tests"]' '[
  {"type": "turn", "tool_calls": [{"name": "write_file",
     "input": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}}]},
  {"type": "turn", "tool_calls": [{"name": "run_tests"}]},
  {"type": "turn", "text": "Fixed add()."}]')
SLOPPY=$(version sloppy '["write_file"]' '[
  {"type": "turn", "tool_calls": [{"name": "write_file",
     "input": {"path": "calc.py", "content": "def add(a, b):\n    return abs(a) + abs(b)\n"}}]},
  {"type": "turn", "text": "Fixed add()."}]')
QUITTER=$(version quitter '["write_file"]' '[{"type": "turn", "text": "I could not fix it."}]')
echo "fixer   $FIXER  (edits the code and runs the tests)"
echo "sloppy  $SLOPPY  (edits the code wrongly, never checks, claims success)"
echo "quitter $QUITTER  (gives up)"

# -- 2. dataset -----------------------------------------------------------------------------
step "2. A dataset of two tasks with hidden checks"
DS=$(post /datasets -d "{\"name\": \"calc-demo\", \"version\": \"$RUN_ID\"}" | jq -r .id)
task() {  # $1 name, $2 evaluation spec (JSON)
  post /tasks -d "{\"name\": \"$1\", \"task_type\": \"agent\", \"image\": \"$IMAGE\",
    \"instructions\": \"The tests fail. Fix calc.py so they pass.\",
    \"test_command\": [\"python\", \"-m\", \"unittest\", \"-q\"],
    \"files\": {\"calc.py\": \"def add(a, b):\n    return a - b\n\",
              \"test_calc.py\": \"import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(2, 3), 5)\n\"},
    \"timeout_s\": 120, \"dataset_id\": \"$DS\", \"difficulty\": \"easy\", \"evaluation\": $2}" |
    jq -c '{id, name, has_evaluation}'
}
task hidden-tests '{
  "checks": [{"kind": "python_tests", "name": "hidden", "pattern": "test_h*.py", "expected_tests": 1}],
  "hidden_files": {"test_hidden.py": "import unittest\nfrom calc import add\nclass H(unittest.TestCase):\n    def test(self):\n        self.assertEqual(add(-1, 1), 0)\n"},
  "protected_paths": ["test_calc.py"]}'
task final-answer '{"checks": [{"kind": "final_answer", "name": "answer", "match": "contains", "expected": "Fixed"}]}'
post "/datasets/$DS/freeze" | jq -c '{status, task_count}'

# -- 3. evaluations -------------------------------------------------------------------------
step "3. Evaluate every agent on the frozen dataset (2 repetitions for the fixer)"
evaluate() {  # $1 label, $2 version id, $3 repetitions -> prints the evaluation id
  post /evaluations -H "Idempotency-Key: demo-$1-$RUN_ID" \
    -d "{\"name\": \"calc-demo-$1\", \"dataset_id\": \"$DS\", \"agent_version_id\": \"$2\", \"repetitions\": $3}" |
    jq -r .id
}
E_FIXER=$(evaluate fixer "$FIXER" 2)
E_SLOPPY=$(evaluate sloppy "$SLOPPY" 1)
E_QUITTER=$(evaluate quitter "$QUITTER" 1)
for E in "$E_FIXER" "$E_SLOPPY" "$E_QUITTER"; do
  tries=0
  until get "/evaluations/$E" | jq -e '.progress.complete' >/dev/null; do
    tries=$((tries + 1))
    [ "$tries" -le 120 ] || { echo "demo.sh: evaluation $E did not finish in 120 s" >&2; exit 1; }
    sleep 1
  done
done
echo "all three evaluations finished"

# -- 4. results -----------------------------------------------------------------------------
step "4. Summaries (success rate with a 95% Wilson interval, failure categories, mean score)"
for pair in "fixer:$E_FIXER" "sloppy:$E_SLOPPY" "quitter:$E_QUITTER"; do
  echo "-- ${pair%%:*}"
  get "/evaluations/${pair#*:}/summary" |
    jq -c '{graded, passed, success_rate, ci95, failure_categories, mean_score: .mean_score.overall}'
done

step "5. One result row: only the public projection (no hidden test names, no expected values)"
get "/evaluations/$E_SLOPPY/results?limit=1" | jq '.results[0] | {status: .run_status, evaluation}'

step "6. Compare the three agents on the same dataset"
get "/evaluations/compare?ids=$E_FIXER,$E_SLOPPY,$E_QUITTER" | jq -c '.per_task[] | {task_id, delta_vs_first}'

# -- sanity: the scripted agents must score as designed, or the stack is broken -----------------
get "/evaluations/$E_FIXER/summary" | jq -e '.graded == 4 and .passed == 4' >/dev/null ||
  { echo "demo.sh: the fixer should pass 4 of 4" >&2; exit 1; }
get "/evaluations/$E_QUITTER/summary" | jq -e '.graded == 2 and .passed == 0' >/dev/null ||
  { echo "demo.sh: the quitter should pass 0 of 2" >&2; exit 1; }

step "Done"
echo "Dashboard (make web-up): $DASH_URL/evaluations/$E_FIXER"
echo "Compare:                 $DASH_URL/evaluations/compare?ids=$E_FIXER,$E_SLOPPY,$E_QUITTER"
echo "EVALUATIONS=$E_FIXER,$E_SLOPPY,$E_QUITTER"
