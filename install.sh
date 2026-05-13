#!/usr/bin/env bash
# =============================================================================
# install.sh — deploy kilo-me globally to ~/.kilo-me/
#
# What this does (idempotent — safe to re-run):
#   1. Verifies/installs uv (Astral's Python package manager)
#   2. Copies .kilo/{agents,rules}/, mcp_servers/, scripts/ to the install root
#   3. Renders kilo.jsonc + mcp.json with absolute paths
#   4. Pre-warms uv's cache for each MCP server
#   5. Initializes the SQLite schema, ChromaDB collection, and Kuzu graph
#   6. Patches state/kilo/model.json to pin per-agent defaults
#   7. Warms the OpenRouter model cache (if a key is already configured)
#   8. Drops a `kilo-me` shim at ~/.local/bin/ so vanilla `kilo` stays untouched
#
# The result: TWO entry points on PATH —
#   * `kilo`      → Kilo Code's stock behavior (reads default ~/.config/kilo/)
#   * `kilo-me`   → Kilo Code with this repo's bundle (reads ~/.kilo-me/...)
#
# The shim simply exports XDG_CONFIG_HOME / XDG_DATA_HOME / XDG_STATE_HOME at
# $KILO_ME_BASE/{config,data,state} and exec's `kilo`. Credentials live at
# $KILO_ME_BASE/data/kilo/auth.json — this script prompts for the OpenRouter
# inference + management keys and writes them there (chmod 600). Set
# KILO_SKIP_AUTH_PROMPT=1 to skip the prompt.
#
# Override the install root with KILO_ME_BASE=/some/path (default ~/.kilo-me).
# Or override individual XDG slots: KILO_HOME, KILO_DATA_HOME, KILO_STATE_HOME.
#
# Windows: run inside WSL2 or Git Bash. Native PowerShell support is not yet
# implemented; the bash dependencies (set -e, uv, sed, sqlite3) are POSIX-only.
# =============================================================================

set -euo pipefail

cd "$(dirname "$0")"
SRC_DIR="$(pwd)"

# Install root. We deliberately do NOT use the system XDG defaults
# (~/.config/kilo etc.) — those belong to vanilla Kilo so users can keep
# running plain `kilo` with stock behavior. The `kilo-me` shim points Kilo at
# this parallel tree via XDG_*_HOME at runtime.
KILO_ME_BASE="${KILO_ME_BASE:-$HOME/.kilo-me}"
KILO_HOME="${KILO_HOME:-$KILO_ME_BASE/config/kilo}"
KILO_DATA_HOME="${KILO_DATA_HOME:-$KILO_ME_BASE/data/kilo}"
KILO_STATE_HOME="${KILO_STATE_HOME:-$KILO_ME_BASE/state/kilo}"
AUTH_FILE="${AUTH_FILE:-$KILO_DATA_HOME/auth.json}"
KILO_ME_SHIM="${KILO_ME_SHIM:-$HOME/.local/bin/kilo-me}"

# -----------------------------------------------------------------------------
# Platform detection + path normalization.
#
# On Git Bash / MSYS, $HOME and $KILO_HOME look like /c/Users/<name>/... — a
# POSIX path only MSYS itself understands. The bash installer reads/writes
# files happily with these paths, but `kilo.exe` (a native Windows process)
# spawned by the user later does NOT translate them. If we bake MSYS paths
# into kilo.jsonc / mcp.json or export them via the shim, Kilo silently fails
# to launch the MCP servers (it spawns `uv run --script /c/Users/.../server.py`
# which native uv.exe can't resolve).
#
# `to_native()` converts a path to Windows mixed-mode (C:/Users/...) on MSYS
# and is a no-op everywhere else. Mixed mode is the right target: forward
# slashes need no JSON escaping AND native Windows tooling accepts them.
# -----------------------------------------------------------------------------
case "$(uname -s 2>/dev/null)" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *)                    IS_WINDOWS=0 ;;
esac

to_native() {
  # Echo $1 in Windows mixed-mode on MSYS, unchanged elsewhere.
  if [ "$IS_WINDOWS" = "1" ] && command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s' "$1"
  fi
}

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
# 2. Layout $KILO_HOME (default: ~/.kilo-me/config/kilo/)
# -----------------------------------------------------------------------------
log "deploying to $KILO_HOME"
mkdir -p "$KILO_HOME"

# If a legacy install lives at the XDG default ~/.config/kilo/, surface a
# migration hint. We never auto-move — the user might rely on that location.
LEGACY_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/kilo"
LEGACY_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/kilo"
if [ -d "$LEGACY_HOME" ] && [ "$LEGACY_HOME" != "$KILO_HOME" ]; then
  warn "legacy install detected at $LEGACY_HOME"
  warn "  vanilla 'kilo' will continue reading it as long as it exists."
  warn "  to migrate it under kilo-me's tree, run (after this installer):"
  warn "    rm -rf '$KILO_HOME' && mv '$LEGACY_HOME' '$KILO_HOME'"
  if [ -f "$LEGACY_DATA/auth.json" ] && [ ! -f "$AUTH_FILE" ]; then
    warn "    mkdir -p '$KILO_DATA_HOME' && mv '$LEGACY_DATA/auth.json' '$AUTH_FILE'"
  fi
fi

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

# Make sure the data home exists (auth.json lives here — populated below by
# prompting for OpenRouter keys, or via `kilo auth login` afterwards).
mkdir -p "$KILO_DATA_HOME"
chmod 700 "$KILO_DATA_HOME"

# -----------------------------------------------------------------------------
# 2b. Prompt for OpenRouter keys and write auth.json
#     - Inference key  → openrouter.key       (used by every agent at runtime)
#     - Management key → OPENROUTER_ADMIN_KEY (used by scripts/project_init.py
#                                              to mint per-project sub-keys)
#     Pre-existing values in auth.json are preserved if the user submits empty
#     input. Set KILO_SKIP_AUTH_PROMPT=1 to bypass entirely (e.g. CI).
# -----------------------------------------------------------------------------
prompt_secret() {
  # $1 = prompt label, $2 = current value (may be empty). Echoes the new value.
  # NOTE: input is NOT hidden — `read -rs` interacts badly with right-click /
  # bracketed-paste in mintty (Git Bash on Windows), so we accept visible echo
  # in exchange for reliable pasting across MSYS/WSL/Linux/macOS terminals.
  local label="$1" current="$2" hint="" answer=""
  if [ -n "$current" ]; then
    hint=" [keep existing]"
  fi
  local prompt_text
  prompt_text="$(printf "${YEL}[install]${NC} %s%s: " "$label" "$hint")"
  if [ -r /dev/tty ]; then
    IFS= read -r -p "$prompt_text" answer < /dev/tty || answer=""
  else
    IFS= read -r -p "$prompt_text" answer || answer=""
  fi
  # Strip any bracketed-paste escape wrappers that mintty/xterm may inject
  # (\e[200~ … \e[201~) and trim trailing CR from Windows line endings.
  answer="${answer#$'\e[200~'}"
  answer="${answer%$'\e[201~'}"
  answer="${answer%$'\r'}"
  # Trim leading/trailing whitespace.
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"
  if [ -z "$answer" ]; then
    printf '%s' "$current"
  else
    printf '%s' "$answer"
  fi
}

if [ "${KILO_SKIP_AUTH_PROMPT:-0}" = "1" ]; then
  log "KILO_SKIP_AUTH_PROMPT=1 — skipping auth.json prompt"
elif [ ! -t 0 ] && [ ! -r /dev/tty ]; then
  warn "non-interactive shell and no /dev/tty — skipping auth.json prompt"
  warn "  run 'kilo auth login' or write $AUTH_FILE manually"
else
  # Pull existing values so re-running install.sh doesn't clobber them on empty input.
  EXISTING_INF=""
  EXISTING_ADMIN=""
  EXISTING_OPENAI=""
  EXISTING_LOCAL_URL=""
  EXISTING_LOCAL_KEY=""
  if [ -f "$AUTH_FILE" ]; then
    EXISTING_INF=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
v = (d.get("openrouter") or {}).get("key") or d.get("OPENROUTER_API_KEY") or ""
print(v)
PYEOF
)
    EXISTING_ADMIN=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
print(d.get("OPENROUTER_ADMIN_KEY", ""))
PYEOF
)
    EXISTING_OPENAI=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
v = (d.get("openai") or {}).get("key") or d.get("OPENAI_API_KEY") or ""
print(v)
PYEOF
)
    EXISTING_LOCAL_URL=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
print((d.get("local") or {}).get("base_url", ""))
PYEOF
)
    EXISTING_LOCAL_KEY=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
print((d.get("local") or {}).get("key", ""))
PYEOF
)
    EXISTING_HARD_URL=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
print((d.get("local-hard") or {}).get("base_url", ""))
PYEOF
)
    EXISTING_HARD_KEY=$(uv run --no-project python - "$AUTH_FILE" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except Exception:
    sys.exit(0)
print((d.get("local-hard") or {}).get("key", ""))
PYEOF
)
  fi

  log "configuring $AUTH_FILE — paste keys (input hidden); press Enter to keep existing"
  OR_INFERENCE_KEY=$(prompt_secret "OpenRouter API key (inference, sk-or-v1-…)" "$EXISTING_INF")
  OR_ADMIN_KEY=$(prompt_secret "OpenRouter management key (sk-or-mgmt-… or sk-or-v1-…)" "$EXISTING_ADMIN")
  OPENAI_KEY=$(prompt_secret "OpenAI API key for task-enricher / gpt-5.4 (sk-…)" "$EXISTING_OPENAI")
  ROUTER_URL=$(prompt_secret "Pi router URL (Phase 3, e.g. http://pi:8080/v1; blank = no router → use the per-tier URLs below)" "")
  if [ -n "$ROUTER_URL" ]; then
    # Router-mode: a single endpoint serves both tiers. The router decides
    # internally. We point BOTH provider blocks at it so any agent's
    # `local/...` or `local-hard/...` model slug works without kilo.jsonc edits.
    LOCAL_URL="$ROUTER_URL"
    HARD_URL="$ROUTER_URL"
    LOCAL_KEY=$(prompt_secret "Router shared token (default 'kilo-local')" "${EXISTING_LOCAL_KEY:-kilo-local}")
    HARD_KEY="$LOCAL_KEY"
    log "  router mode — local and local-hard both → $ROUTER_URL"
  else
    LOCAL_URL=$(prompt_secret "Soft-tier base URL (Mac mini, e.g. http://macmini:11434/v1; blank = no cluster)" "$EXISTING_LOCAL_URL")
    LOCAL_KEY=$(prompt_secret "Soft-tier shared token (any non-empty string; default 'kilo-local')" "${EXISTING_LOCAL_KEY:-kilo-local}")
    HARD_URL=$(prompt_secret "Hard-tier base URL (RTX, e.g. http://rtx:11434/v1; blank = soft-tier only)" "$EXISTING_HARD_URL")
    HARD_KEY=$(prompt_secret "Hard-tier shared token (default 'kilo-local')" "${EXISTING_HARD_KEY:-kilo-local}")
  fi

  if [ -z "$OR_INFERENCE_KEY" ] && [ -z "$OR_ADMIN_KEY" ] && [ -z "$OPENAI_KEY" ] && [ -z "$LOCAL_URL" ] && [ -z "$HARD_URL" ]; then
    warn "  no keys provided — leaving auth.json untouched"
  else
    AUTH_FILE="$AUTH_FILE" \
    OR_INFERENCE_KEY="$OR_INFERENCE_KEY" \
    OR_ADMIN_KEY="$OR_ADMIN_KEY" \
    OPENAI_KEY="$OPENAI_KEY" \
    LOCAL_URL="$LOCAL_URL" \
    LOCAL_KEY="$LOCAL_KEY" \
    HARD_URL="$HARD_URL" \
    HARD_KEY="$HARD_KEY" \
    uv run --no-project python - <<'PYEOF'
import json, os, pathlib
path = pathlib.Path(os.environ["AUTH_FILE"])
inf = os.environ.get("OR_INFERENCE_KEY", "")
admin = os.environ.get("OR_ADMIN_KEY", "")
oai = os.environ.get("OPENAI_KEY", "")
local_url = os.environ.get("LOCAL_URL", "")
local_key = os.environ.get("LOCAL_KEY", "")
hard_url = os.environ.get("HARD_URL", "")
hard_key = os.environ.get("HARD_KEY", "")
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {}
if inf:
    # Kilo CLI requires the provider object to declare {"type": "api"} —
    # without it the runtime ignores the key and falls back to free models.
    prov = data.setdefault("openrouter", {})
    prov["type"] = "api"
    prov["key"] = inf
if admin:
    data["OPENROUTER_ADMIN_KEY"] = admin
if oai:
    # Same shape as openrouter so Kilo treats it as a first-class provider,
    # plus the flat OPENAI_API_KEY for the task-enricher MCP server.
    prov = data.setdefault("openai", {})
    prov["type"] = "api"
    prov["key"] = oai
    data["OPENAI_API_KEY"] = oai
if local_url:
    # Soft-tier local cluster (Mac mini via Tailscale → Ollama).
    # type "openai-compat" tells Kilo this is an OpenAI-shape endpoint
    # at a non-standard URL.
    prov = data.setdefault("local", {})
    prov["type"] = "openai-compat"
    prov["key"] = local_key or "kilo-local"
    prov["base_url"] = local_url.rstrip("/")
if hard_url:
    # Hard-tier local cluster (RTX via Tailscale → Ollama). architect-lo /
    # coder-lo / debugger-lo land here. Phase 3 router will make this dynamic.
    prov = data.setdefault("local-hard", {})
    prov["type"] = "openai-compat"
    prov["key"] = hard_key or "kilo-local"
    prov["base_url"] = hard_url.rstrip("/")
path.write_text(json.dumps(data, indent=2) + "\n")
try:
    os.chmod(path, 0o600)
except OSError:
    pass
PYEOF
    log "  wrote $AUTH_FILE (chmod 600)"
  fi
fi

# -----------------------------------------------------------------------------
# 3. Render kilo.jsonc + mcp.json with absolute paths
# -----------------------------------------------------------------------------
render() {
  local src="$1" dst="$2"
  # Replace __KILO_HOME__ with the actual path. sed -i differs across BSD/GNU,
  # so we use a temp file. We feed in the *native* form so Windows kilo.exe
  # can spawn uv with an absolute path it actually understands (mixed-mode
  # C:/Users/... rather than the MSYS-only /c/Users/... form).
  sed "s|__KILO_HOME__|${KILO_HOME_NATIVE}|g" "$src" > "$dst.tmp" && mv "$dst.tmp" "$dst"
}

KILO_HOME_NATIVE="$(to_native "$KILO_HOME")"
render "$SRC_DIR/.kilo/kilo.jsonc" "$KILO_HOME/kilo.jsonc"
render "$SRC_DIR/.kilo/mcp.json"   "$KILO_HOME/mcp.json"
log "rendered kilo.jsonc and mcp.json with KILO_HOME=$KILO_HOME_NATIVE"

# fallbacks.json — Rule 06 model fallback chains. User-editable; preserve any
# local edits across re-installs by only writing the file when it's absent.
if [ -f "$KILO_HOME/fallbacks.json" ]; then
  log "kept existing $KILO_HOME/fallbacks.json (re-install preserves your edits)"
else
  cp "$SRC_DIR/.kilo/fallbacks.json" "$KILO_HOME/fallbacks.json"
  log "wrote $KILO_HOME/fallbacks.json (5-model fallback chain per agent)"
fi

# Make scripts executable
chmod +x "$KILO_HOME"/mcp_servers/*/server.py 2>/dev/null || true
chmod +x "$KILO_HOME"/mcp_servers/mermaid_vector/chroma_init.py 2>/dev/null || true
chmod +x "$KILO_HOME"/scripts/*.py 2>/dev/null || true

# -----------------------------------------------------------------------------
# 4. Pre-warm uv cache for each MCP server
#    (forces dep download once, so the first MCP call doesn't time out)
# -----------------------------------------------------------------------------
log "pre-warming uv caches for MCP servers (first run downloads deps)"
for srv in sqlite_memory openrouter_models mermaid_vector graph_memory task_enricher cluster_health; do
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
# 7. Patch $KILO_STATE_HOME/model.json — pin built-in agents to -ch models
#    Without this, Kilo falls through to a free-tier alias for the default
#    code mode even when kilo.jsonc sets a different model.
# -----------------------------------------------------------------------------
MODEL_JSON="$KILO_STATE_HOME/model.json"

log "patching $MODEL_JSON — pinning -ch agents"
mkdir -p "$KILO_STATE_HOME"
# Use `uv run python` so we don't depend on a `python3` binary being on PATH —
# on Windows the bare `python3` invocation hits the Microsoft Store shim when
# Python is installed as `python.exe` / via the `py` launcher. uv was already
# verified at step 1, so this is portable across Linux/macOS/Git Bash.
uv run --no-project python - "$MODEL_JSON" <<'PYEOF'
import json, sys

path = sys.argv[1]
try:
    with open(path) as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}

m = state.setdefault("model", {})
# Built-in slots — LOCAL-first, hard tier. The "local-hard" providerID maps
# to the RTX worker declared in auth.json. Rule 06 fallbacks walk
# local-hard → local (soft, Mac mini) → -ch / -us on cluster outage.
m["code"]              = {"providerID": "local-hard", "modelID": "qwen3-coder:14b-instruct-q8_0"}
m["architect"]         = {"providerID": "local-hard", "modelID": "devstral:22b-q5_K_M"}
m["debug"]             = {"providerID": "local-hard", "modelID": "deepseek-r1-distill-qwen:14b-q5_K_M"}
m["ask"]               = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v3.2:free"}
m["plan"]              = {"providerID": "openrouter", "modelID": "minimax/minimax-m2.7"}

# Chinese-default lineup
m["coder-ch"]          = {"providerID": "openrouter", "modelID": "moonshotai/kimi-k2.6"}
m["architect-ch"]      = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v4-pro"}
m["debugger-ch"]       = {"providerID": "openrouter", "modelID": "z-ai/glm-5.1"}
m["memory-curator-ch"] = {"providerID": "openrouter", "modelID": "qwen/qwen3.6-plus"}
m["cheap-fallback-ch"] = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v3.2"}
m["accountant-ch"]     = {"providerID": "openrouter", "modelID": "deepseek/deepseek-v3.2"}

# Western frontier lineup
m["coder-us"]          = {"providerID": "openai",     "modelID": "gpt-5.4"}
m["architect-us"]      = {"providerID": "openrouter", "modelID": "anthropic/claude-opus-4.7"}
m["debugger-us"]       = {"providerID": "openrouter", "modelID": "anthropic/claude-sonnet-4.6"}
m["memory-curator-us"] = {"providerID": "openrouter", "modelID": "anthropic/claude-haiku-4.5"}
m["cheap-fallback-us"] = {"providerID": "openrouter", "modelID": "x-ai/grok-4.1-fast"}
m["accountant-us"]     = {"providerID": "openrouter", "modelID": "anthropic/claude-haiku-4.5"}

# Local cluster lineup — Ollama via Tailscale (Rule 07). Phase 2: hard tier
# on "local-hard" (RTX), soft tier on "local" (Mac mini). Phase 3 will fold
# these back into a single "local" provider once the Pi router can decide.
m["architect-lo"]      = {"providerID": "local-hard", "modelID": "devstral:22b-q5_K_M"}
m["coder-lo"]          = {"providerID": "local-hard", "modelID": "qwen3-coder:14b-instruct-q8_0"}
m["debugger-lo"]       = {"providerID": "local-hard", "modelID": "deepseek-r1-distill-qwen:14b-q5_K_M"}
m["memory-curator-lo"] = {"providerID": "local",      "modelID": "llama3.3:8b-instruct-q5_K_M"}
m["cheap-fallback-lo"] = {"providerID": "local",      "modelID": "gemma3:4b"}
m["accountant-lo"]     = {"providerID": "local",      "modelID": "llama3.3:8b-instruct-q5_K_M"}

with open(path, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PYEOF
log "  ok: $MODEL_JSON"

# -----------------------------------------------------------------------------
# 8. Generate the `kilo-me` shim on PATH
#    Lets users keep `kilo` for stock Kilo behavior and use `kilo-me` to launch
#    Kilo with this repo's bundle injected via XDG_*_HOME.
# -----------------------------------------------------------------------------
log "writing $KILO_ME_SHIM"
mkdir -p "$(dirname "$KILO_ME_SHIM")"

# On Windows, `kilo` is `kilo.exe` and won't translate MSYS-style env vars.
# Bake the native (mixed-mode) path of $KILO_ME_BASE into the shim so the
# XDG_*_HOME values it exports are something kilo.exe can resolve.
KILO_ME_BASE_NATIVE="$(to_native "$KILO_ME_BASE")"

cat > "$KILO_ME_SHIM" <<SHIM
#!/usr/bin/env bash
# kilo-me — launch Kilo Code with the kilo-me bundle.
# Generated by install.sh; safe to regenerate by re-running the installer.
set -e
KILO_ME_BASE="\${KILO_ME_BASE:-$KILO_ME_BASE_NATIVE}"
export XDG_CONFIG_HOME="\$KILO_ME_BASE/config"
export XDG_DATA_HOME="\$KILO_ME_BASE/data"
export XDG_STATE_HOME="\$KILO_ME_BASE/state"
export KILO_HOME="\$XDG_CONFIG_HOME/kilo"
export AUTH_FILE="\$XDG_DATA_HOME/kilo/auth.json"
exec kilo "\$@"
SHIM
chmod +x "$KILO_ME_SHIM"
log "  ok: $KILO_ME_SHIM"

# Windows: drop a CMD companion alongside the bash shim so PowerShell / cmd.exe
# users can launch kilo-me without going through Git Bash. The .cmd uses the
# same XDG_*_HOME contract as the bash shim, just in Windows-native form.
if [ "$IS_WINDOWS" = "1" ]; then
  KILO_ME_SHIM_CMD="${KILO_ME_SHIM}.cmd"
  # Convert to backslash form for the CMD batch script — cmd.exe doesn't
  # consistently expand forward slashes in %VAR% paths during set.
  if command -v cygpath >/dev/null 2>&1; then
    KILO_ME_BASE_WIN="$(cygpath -w "$KILO_ME_BASE")"
  else
    KILO_ME_BASE_WIN="$KILO_ME_BASE_NATIVE"
  fi
  log "writing $KILO_ME_SHIM_CMD (Windows launcher)"
  cat > "$KILO_ME_SHIM_CMD" <<CMD
@echo off
REM kilo-me — launch Kilo Code with the kilo-me bundle (Windows).
REM Generated by install.sh; safe to regenerate by re-running the installer.
if "%KILO_ME_BASE%"=="" set "KILO_ME_BASE=${KILO_ME_BASE_WIN}"
set "XDG_CONFIG_HOME=%KILO_ME_BASE%\\config"
set "XDG_DATA_HOME=%KILO_ME_BASE%\\data"
set "XDG_STATE_HOME=%KILO_ME_BASE%\\state"
set "KILO_HOME=%XDG_CONFIG_HOME%\\kilo"
set "AUTH_FILE=%XDG_DATA_HOME%\\kilo\\auth.json"
kilo.exe %*
CMD
  log "  ok: $KILO_ME_SHIM_CMD"
fi

# Warn if ~/.local/bin isn't on PATH so the shim is discoverable.
case ":${PATH:-}:" in
  *":$(dirname "$KILO_ME_SHIM"):"*) ;;
  *) warn "$(dirname "$KILO_ME_SHIM") is not on PATH — add this to your shell rc:"
     warn "  export PATH=\"$(dirname "$KILO_ME_SHIM"):\$PATH\""
     ;;
esac

# -----------------------------------------------------------------------------
# 9. Done
# -----------------------------------------------------------------------------
cat <<EOF

${GRN}===== install complete =====${NC}

  KILO_ME_BASE    = $KILO_ME_BASE
  KILO_HOME       = $KILO_HOME
  KILO_DATA_HOME  = $KILO_DATA_HOME
  KILO_STATE_HOME = $KILO_STATE_HOME

  Config       : $KILO_HOME/kilo.jsonc
  Agents       : $KILO_HOME/agents/        (architect/coder/debugger/memory-curator/accountant × ch+us+lo, plus ask)
  Rules        : $KILO_HOME/rules/         (01–07; 05 = enrichment, 06 = fallbacks, 07 = local-cluster routing)
  MCP servers  : $KILO_HOME/mcp_servers/   (sqlite-memory, openrouter-models, mermaid-vector, graph-memory, task-enricher, cluster-health)
  Fallbacks    : $KILO_HOME/fallbacks.json (per-agent 5-model chain — Rule 06)
  Cluster      : $SRC_DIR/cluster/          (workers/setup-{macmini,rtx}.sh; router/setup-pi.sh)
  Memory DB    : $KILO_HOME/memory.sqlite
  ChromaDB     : $KILO_HOME/chroma/
  Graph DB     : $KILO_HOME/graph.kuzu/
  Model cache  : $KILO_HOME/models.curated.json
  Credentials  : $AUTH_FILE  ${YEL}(written above; rerun installer to update)${NC}
  Wrapper      : $KILO_ME_SHIM

Two entry points on PATH:
  ${GRN}kilo${NC}      → vanilla Kilo Code (reads ~/.config/kilo/, untouched by this installer)
  ${GRN}kilo-me${NC}   → Kilo Code launched against $KILO_ME_BASE/

Next steps:
  1. (Optional) verify auth.json — both keys should be present:
       make auth-status

  2. Launch from any project:
       cd ~/my-project
       kilo-me

  3. Daily model refresh (auth.json is read automatically):
       0 6 * * *  uv run $KILO_HOME/scripts/refresh_models.py >> $KILO_HOME/refresh.log 2>&1

To uninstall: bash uninstall.sh           (keeps user data + auth.json)
              bash uninstall.sh --purge   (removes everything, incl. the shim)
EOF
