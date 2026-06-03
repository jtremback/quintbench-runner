#!/usr/bin/env bash
# One-task smoke test for QuintMiniSweAgent under Pier.
#
# Usage:
#   ./scripts/smoke.sh [task-path] [model]
#
# Defaults:
#   task-path  ../deepswe-datacurve/tasks/pebble-durability-wait-apis
#   model      bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0
#
# Auth — two modes, selected by the model prefix:
#
#   bedrock/...   Uses AWS Bedrock. Exports SSO credentials from the
#                 AWS profile in $AWS_PROFILE (default: bedrock) and forwards
#                 them into the sandbox. Run `aws sso login --profile bedrock`
#                 first if your token has expired. No ANTHROPIC_API_KEY needed.
#
#   anthropic/... Uses the Anthropic API directly. Requires ANTHROPIC_API_KEY.
#                 Use sk-fake-for-smoke-test for a plumbing-only smoke (the
#                 whole install chain runs, then the agent fails predictably at
#                 the first LLM call).
#
# Requires:
#   - Docker daemon running (or change --env)
#   - `pip install -e .` already run in a venv at ./.venv
#   - A sibling clone of github.com/jtremback/quint-skill at ../quint-skill,
#     or QUINT_SKILL_PATH pointing elsewhere
#   - For bedrock: AWS CLI v2 + a configured SSO profile (see the cycles
#     claude-code-on-bedrock doc).

set -euo pipefail

TASK_PATH="${1:-../deepswe-datacurve/tasks/pebble-durability-wait-apis}"
MODEL="${2:-bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0}"

case "$MODEL" in
  bedrock/*)
    AWS_PROFILE="${AWS_PROFILE:-bedrock}"
    export AWS_REGION="${AWS_REGION:-us-east-1}"
    if ! aws sts get-caller-identity --profile "$AWS_PROFILE" >/dev/null 2>&1; then
      echo "ERROR: AWS profile '$AWS_PROFILE' has no valid session." >&2
      echo "Run: aws sso login --profile $AWS_PROFILE" >&2
      exit 1
    fi
    # Export temporary SSO credentials into this shell's env. The runner
    # forwards AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
    # into the sandbox for bedrock models.
    eval "$(aws configure export-credentials --profile "$AWS_PROFILE" --format env)"
    echo "Bedrock auth: profile=$AWS_PROFILE region=$AWS_REGION model=$MODEL"
    ;;
  *)
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
      cat >&2 <<'EOF'
ERROR: ANTHROPIC_API_KEY not set for a non-bedrock model.
Set a real key for a real run, or 'sk-fake-for-smoke-test' for a plumbing-only
smoke. Or pass a bedrock/... model to use AWS Bedrock instead.
EOF
      exit 1
    fi
    ;;
esac

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
