#!/usr/bin/env bash
# Start Claude Code with this checkout's plugin and nothing else: no other
# plugins, no claude.ai connectors. Data goes to ~/.canvas-context-dev unless
# CANVAS_CONTEXT_HOME says otherwise; point CANVAS_CONTEXT_MODELS_DIR at a folder that already has the
# model to skip the download.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export CANVAS_CONTEXT_HOME="${CANVAS_CONTEXT_HOME:-$HOME/.canvas-context-dev}"
export CANVAS_CONTEXT_MODELS_DIR="${CANVAS_CONTEXT_MODELS_DIR:-$CANVAS_CONTEXT_HOME/models}"
export ENABLE_CLAUDEAI_MCP_SERVERS=false
"$ROOT/plugin/scripts/server" --version >/dev/null  # warm uvx's cache, so the server starts in time
exec claude --plugin-dir "$ROOT/plugin" --setting-sources '' "$@"
