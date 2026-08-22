#!/usr/bin/env bash
# Run the full briefing pipeline, then Strix, in one command.
#
#   1. Generate the AgentDojo briefing (docs/agentdojo/agentdojo_to_briefing.py)
#   2. Generate the InjecAgent briefing (docs/injecagent/injecagent_to_briefing.py)
#   3. Merge both into one instruction file (docs/merge_briefings.py)
#   4. Run `strix` with that instruction file against the target
#
# Usage:
#   ./docs/run_briefed_scan.sh --target <url> [any other strix flags]
#
# Example:
#   export STRIX_LLM="openai/gpt-5.4"
#   export LLM_API_KEY="..."
#   ./docs/run_briefed_scan.sh --target https://example.com --scan-mode quick
#
# Do not pass --instruction or --instruction-file yourself — this script
# supplies --instruction-file with the combined briefing; anything you pass
# after it will be overridden (argparse keeps the last occurrence).
#
# Always runs strix with -n/--non-interactive: running from source via `uv
# run` has no Bubble Tea TUI binary (that's only bundled in official platform
# wheels), so interactive mode can't start here anyway, and non-interactive
# is the right mode for a scripted pipeline regardless.
#
# Requires: `uv sync --group agentdojo-tools` run at least once (only the
# AgentDojo half needs it; InjecAgent's generator has no extra dependencies).
# Requires Docker running and STRIX_LLM/LLM_API_KEY set, same as any Strix run.

set -euo pipefail

# A stale VIRTUAL_ENV from an unrelated activated venv otherwise makes every
# `uv run` print a harmless-but-noisy mismatch warning.
unset VIRTUAL_ENV

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${BRIEFING_OUT_DIR:-$REPO_ROOT/briefings}"
mkdir -p "$OUT_DIR"

AGENTDOJO_JSON="$OUT_DIR/agentdojo_briefing.json"
INJECAGENT_JSON="$OUT_DIR/injecagent_briefing.json"
COMBINED_MD="$OUT_DIR/combined_briefing.md"

echo "== [1/4] AgentDojo briefing ==" >&2
if ! uv run --project "$REPO_ROOT" python "$REPO_ROOT/docs/agentdojo/agentdojo_to_briefing.py" \
    --out "$AGENTDOJO_JSON"; then
  echo "AgentDojo briefing generation failed." >&2
  echo "Is the optional dependency installed? Run: uv sync --group agentdojo-tools" >&2
  exit 1
fi

echo "== [2/4] InjecAgent briefing ==" >&2
uv run --project "$REPO_ROOT" python "$REPO_ROOT/docs/injecagent/injecagent_to_briefing.py" \
  --out "$INJECAGENT_JSON"

echo "== [3/4] Merging into one instruction file ==" >&2
uv run --project "$REPO_ROOT" python "$REPO_ROOT/docs/merge_briefings.py" \
  --briefing "$AGENTDOJO_JSON" --briefing "$INJECAGENT_JSON" --out "$COMBINED_MD"

echo "== [4/4] Running Strix ==" >&2
exec uv run --project "$REPO_ROOT" strix "$@" --instruction-file "$COMBINED_MD" --non-interactive
