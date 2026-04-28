#!/usr/bin/env bash
# =============================================================================
# install.sh — deploy kilo-me globally to ~/.config/kilo/
#
# What this does (idempotent — safe to re-run):
#   1. Verifies/installs uv (Astral's Python package manager)
#   2. Copies .kilo/{agents,rules}/, mcp_servers/, scripts/ to ~/.config/kilo/
#   3. Renders kilo.jsonc + mcp.json with absolute paths
#   4. Pre-warms uv's cache for each MCP server
#   5. Initializes the SQLite schema, ChromaDB collection, and Kuzu graph
#   6. Patches ~/.local/state/kilo/model.json to pin per-agent defaults
#   7. Warms the OpenRouter model cache (if a key is already configured)
#
# The result: Kilo Code reads ~/.config/kilo/kilo.jsonc globally for every
# project. Credentials live separately at ~/.local/share/kilo/auth.json — run
# `kilo auth login` after this script completes to populate it.
#
# Override locations: KILO_HOME, XDG_CONFIG_HOME, XDG_DATA_HOME.
#
# Windows: run inside WSL2 or Git Bash. Native PowerShell support is not yet
# implemented; the bash dependencies (set -e, uv, sed, sqlite3) are POSIX-only.
# =============================================================================

set -euo pipefail

cd "$(dirname "$0")"
SRC_DIR="$(pwd)"

# Default install location follows the XDG Base Directory spec:
#   $XDG_CONFIG_HOME/kilo  (typically ~/.config/kilo) — code, agents, MCP servers
#   $XDG_DATA_HOME/kilo    (typically ~/.local/share/kilo) — credentials
# Override with KILO_HOME=/some/path before running this script.
XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
KILO_HOME="${KILO_HOME:-$XDG_CONFIG_HOME/kilo}"
KILO_DATA_HOME="${KILO_DATA_HOME:-$XDG_DATA_HOME/kilo}"
AUTH_FILE="${AUTH_FILE:-$KILO_DATA_HOME/auth.json}"

# ANSI-C quoting: $'...' interprets \033 at definition time, so the variable
# holds the actual ESC byte. This lets the value render correctly via both
# printf (used in log/warn/die) AND heredocs (used in the completion banner).
YEL=$'\033[0;33m'
GRN=$'\033[0;32m'
RED=$'\033[0;31m'
NC=$'\033[0m'

log()  { printf "${GRN}[install]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[install]${NC} %s\n" "$*"; }
die()  { printf "${RED}[install]${NC} %s\n" "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# 1. uv check / install
# -----------------------------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  log "uv not found — installing from astral.sh"
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    die "neither curl nor wget available — install uv manually: https://docs.astral.sh/uv/"
  fi
  # uv installer drops the binary in ~/.local/bin or ~/.cargo/bin — make sure
  # it's on PATH for the rest of this script.
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
log "uv: $(uv --version)"

# -----------------------------------------------------------------------------
# 2. Layout $KILO_HOME (default: ~/.config/kilo/)
# -----------------------------------------------------------------------------
log "deploying to $KILO_HOME"
mkdir -p "$KILO_HOME"

# Backup existing kilo.jsonc if present (re-install scenario)
if [ -f "$KILO_HOME/kilo.jsonc" ]; then
  ts=$(date +%Y%m%d-%H%M%S)
  cp "$KILO_HOME/kilo.jsonc" "$KILO_HOME/kilo.jsonc.bak.$ts"
  log "backed up existing config to kilo.jsonc.bak.$ts"
fi

# Copy directories — use rsync if available, else cp -R
copy_dir() {
  local src="$1" dst="$2"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$src/" "$dst/"
  else
    rm -rf "$dst" && mkdir -p "$dst" && cp -R "$src/." "$dst/"
  fi
}

copy_dir "$SRC_DIR/.kilo/agents" "$KILO_HOME/agents"
copy_dir "$SRC_DIR/.kilo/rules"  "$KILO_HOME/rules"
copy_dir "$SRC_DIR/mcp_servers"  "$KILO_HOME/mcp_servers"
copy_dir "$SRC_DIR/scripts"      "$KILO_HOME/scripts"

# decisions/ + adr/ are user data — only create if absent
mkdir -p "$KILO_HOME/decisions" "$KILO_HOME/adr"

# Make sure the data home exists (auth.json lives here once `kilo auth login`
# runs — this script no longer bootstraps it from env vars).
mkdir -p "$KILO_DATA_HOME"
chmod 700 "$KILO_DATA_HOME"

# -----------------------------------------------------------------------------
# 3. Render kilo.jsonc + mcp.json with absolute paths
# -----------------------------------------------------------------------------
render() {
  local src="$1" dst="$2"
  # Replace __KILO_HOME__ with the actual path. sed -i differs across BSD/GNU,
  # so we use a temp file.
  sed "s|__KILO_HOME__|${KILO_HOME}|g" "$src" > "$dst.tmp" && mv "$dst.tmp" "$dst"
}

render "$SRC_DIR/.kilo/kilo.jsonc" "$KILO_HOME/kilo.jsonc"
render "$SRC_DIR/.kilo/mcp.json"   "$KILO_HOME/mcp.json"
log "rendered kilo.jsonc and mcp.json with KILO_HOME=$KILO_HOME"

# Make scripts executable
chmod +x "$KILO_HOME"/mcp_servers/*/server.py 2>/dev/null || true
chmod +x "$KILO_HOME"/mcp_servers/mermaid_vector/chroma_init.py 2>/dev/null || true
chmod +x "$KILO_HOME"/scripts/*.py 2>/dev/null || true

# -----------------------------------------------------------------------------
# 4. Pre-warm uv cache for each MCP server
#    (forces dep download once, so the first MCP call doesn't time out)
# -----------------------------------------------------------------------------
log "pre-warming uv caches for MCP servers (first run downloads deps)"
for srv in sqlite_memory openrouter_models mermaid_vector graph_memory; do
  printf "  - %-20s " "$srv"
  # `timeout` lets uv resolve+download deps, then kills the blocking MCP server
  # process before it hangs waiting on stdin. Exit code 124 = timed out = ok.
  timeout 60 uv run --script "$KILO_HOME/mcp_servers/$srv/server.py" >/dev/null 2>&1 || true
  echo "ok"
done

# -----------------------------------------------------------------------------
# 5. Initialize stores
# -----------------------------------------------------------------------------
log "initializing sqlite-memory schema"
sqlite3 "$KILO_HOME/memory.sqlite" < "$KILO_HOME/mcp_servers/sqlite_memory/schema.sql"
log "  ok: $KILO_HOME/memory.sqlite"

log "initializing chromadb collection"
CHROMA_PATH="$KILO_HOME/chroma" \
  uv run --script "$KILO_HOME/mcp_servers/mermaid_vector/chroma_init.py" init || \
  warn "chromadb init failed — first run may take longer to download deps"

log "initializing kuzu graph store"
GRAPH_DB_PATH="$KILO_HOME/graph.kuzu" \
  uv run --script "$KILO_HOME/mcp_servers/graph_memory/server.py" --init || \
  warn "kuzu init failed — first run may take longer to download deps"

# -----------------------------------------------------------------------------
# 6. Warm OpenRouter cache (only if key already in env or auth.json)
# -----------------------------------------------------------------------------
# The refresh script self-loads auth.json, so this works once the user has
# completed `kilo auth login`. On a first install the key is usually missing —
# that's expected; the warning is informational.
log "warming OpenRouter model cache"
if uv run --script "$KILO_HOME/scripts/refresh_models.py" >/dev/null 2>&1; then
  log "  ok"
else
  warn "  refresh skipped — run 'kilo auth login' to write auth.json,"
  warn "  then: uv run $KILO_HOME/scripts/refresh_models.py"
fi

# -----------------------------------------------------------------------------
# 7. Patch ~/.local/state/kilo/model.json — pin built-in agents to -ch models
#    Without this, Kilo falls through to a free-tier alias for the default
#    code mode even when kilo.jsonc sets a different model.
# -----------------------------------------------------------------------------
XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
KILO_STATE_HOME="${KILO_STATE_HOME:-$XDG_STATE_HOME/kilo}"
MODEL_JSON="$KILO_STATE_HOME/model.json"

log "patching $MODEL_JSON — pinning -ch agents"
mkdir -p "$KILO_STATE_HOME"
python3 - "$MODEL_JSON" <<'PYEOF'
import json, sys

path = sys.argv[1]
try:
    with open(path) as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

m = state.setdefault("model", {})
# Built-in slots
m["code"]              = {"providerID": "openrouter", "modelID": "moonshotai/kimi-k2.6"}
m["architect"]         = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v4-pro"}
m["debug"]             = {"providerID": "openrouter", "modelID": "z-ai/glm-5.1"}
m["ask"]               = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v3.2:free"}
m["plan"]              = {"providerID": "openrouter", "modelID": "minimax/minimax-m2.7"}

# Chinese-default lineup
m["coder-ch"]          = {"providerID": "openrouter", "modelID": "moonshotai/kimi-k2.6"}
m["architect-ch"]      = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v4-pro"}
m["debugger-ch"]       = {"providerID": "openrouter", "modelID": "z-ai/glm-5.1"}
m["memory-curator-ch"] = {"providerID": "openrouter", "modelID": "qwen/qwen3.6-plus"}
m["cheap-fallback-ch"] = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v3.2"}

# Western frontier lineup
m["coder-us"]          = {"providerID": "openai",     "modelID": "gpt-5.4"}
m["architect-us"]      = {"providerID": "openrouter", "modelID": "anthropic/claude-opus-4.7"}
m["debugger-us"]       = {"providerID": "openrouter", "modelID": "anthropic/claude-sonnet-4.6"}
m["memory-curator-us"] = {"providerID": "openrouter", "modelID": "anthropic/claude-haiku-4.5"}
m["cheap-fallback-us"] = {"providerID": "openrouter", "modelID": "x-ai/grok-4.1-fast"}

with open(path, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PYEOF
log "  ok: $MODEL_JSON"

# -----------------------------------------------------------------------------
# 8. Done
# -----------------------------------------------------------------------------
cat <<EOF

${GRN}===== install complete =====${NC}

  KILO_HOME      = $KILO_HOME
  KILO_DATA_HOME = $KILO_DATA_HOME

  Config       : $KILO_HOME/kilo.jsonc
  Agents       : $KILO_HOME/agents/        (architect/coder/debugger/memory-curator × ch+us, plus ask)
  Rules        : $KILO_HOME/rules/
  MCP servers  : $KILO_HOME/mcp_servers/   (sqlite-memory, openrouter-models, mermaid-vector, graph-memory)
  Memory DB    : $KILO_HOME/memory.sqlite
  ChromaDB     : $KILO_HOME/chroma/
  Graph DB     : $KILO_HOME/graph.kuzu/
  Model cache  : $KILO_HOME/models.curated.json
  Credentials  : $AUTH_FILE  ${YEL}(populate via 'kilo auth login')${NC}

Next steps:
  1. Authenticate with OpenRouter (Kilo's built-in flow writes the provider
     object into auth.json with chmod 600):
       kilo auth login

  2. Start Kilo from any project — no wrapper required:
       cd ~/my-project
       kilo

  3. Add a daily cron line for the model refresh (auth.json is read automatically):
       0 6 * * *  uv run $KILO_HOME/scripts/refresh_models.py >> $KILO_HOME/refresh.log 2>&1

To uninstall: bash uninstall.sh           (keeps user data + auth.json)
              bash uninstall.sh --purge   (removes everything)
EOF
