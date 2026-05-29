#!/usr/bin/env bash
# One-task smoke test for QuintMiniSweAgent under Pier.
#
# Usage:
#   ANTHROPIC_API_KEY=<key> ./scripts/smoke.sh [task-path] [model]
#
# Defaults:
#   task-path  ../deepswe-datacurve/tasks/pebble-durability-wait-apis
#   model      anthropic/claude-haiku-4-5
#
# Use sk-fake-for-smoke-test as the key for a plumbing-only smoke: the
# entire install chain (apt nodejs + npm quint + skill materialization +
# mini-swe-agent install + prompt injection + agent launch) executes, then
# the agent fails predictably at the first LLM call.
#
# Requires:
#   - Docker daemon running (or change --env)
#   - `pip install -e .` already run in a venv at ./.venv
#   - A sibling clone of github.com/jtremback/quint-skill at ../quint-skill,
#     or QUINT_SKILL_PATH pointing elsewhere

set -euo pipefail

TASK_PATH="${1:-../deepswe-datacurve/tasks/pebble-durability-wait-apis}"
MODEL="${2:-anthropic/claude-haiku-4-5}"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  cat >&2 <<'EOF'
ERROR: ANTHROPIC_API_KEY not set.
Set a real key for a real run, or 'sk-fake-for-smoke-test' for a plumbing-only smoke.
EOF
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIER_BIN="$REPO_ROOT/.venv/bin/pier"
if [ ! -x "$PIER_BIN" ]; then
  echo "ERROR: pier not found at $PIER_BIN — run 'pip install -e .' in a venv first." >&2
  exit 1
fi

cd "$REPO_ROOT"
exec "$PIER_BIN" run \
  -p "$TASK_PATH" \
  --agent-import-path quintbench_runner.pier_agent:QuintMiniSweAgent \
  --model "$MODEL" \
  --env docker \
  --n-concurrent 1
