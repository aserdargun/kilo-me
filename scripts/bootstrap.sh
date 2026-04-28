#!/usr/bin/env bash
# =============================================================================
# bootstrap.sh — DEV-mode setup (project-local, for working ON the stack)
#
# For end users who just want kilo working globally, run install.sh instead.
# This script is for developers iterating on the MCP servers + tests.
#
# What it does:
#   1. Verifies/installs uv
#   2. Creates a uv-managed venv with pyproject.toml dev deps
#   3. Primes a local SQLite DB at .kilo/memory.sqlite (project-scoped)
#   4. Initializes a local ChromaDB at .kilo/chroma
#   5. Runs the test suite
# =============================================================================

set -euo pipefail
cd "$(dirname "$0")/.."

YEL='\033[0;33m'
GRN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log()  { printf "${GRN}[bootstrap]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[bootstrap]${NC} %s\n" "$*"; }
die()  { printf "${RED}[bootstrap]${NC} %s\n" "$*" >&2; exit 1; }

# 1. uv
if ! command -v uv >/dev/null 2>&1; then
  log "installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
log "uv: $(uv --version)"

# 2. Project venv with dev deps
log "creating venv + installing dev deps via uv"
uv venv --quiet
uv pip install -e ".[dev]" --quiet

# 3. Load .env if present
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f .env ]; then
  set -a; source .env; set +a
fi

# 4. Init project-local stores
mkdir -p .kilo

log "priming sqlite-memory schema (project-local)"
MEMORY_DB_PATH=.kilo/memory.sqlite uv run python -c "
import sys, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('sm', 'mcp_servers/sqlite_memory/server.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
mod._ensure_schema()
print(f'  ok: {mod._DB_PATH}')
"

log "initializing chromadb collection (project-local)"
CHROMA_PATH=.kilo/chroma uv run mcp_servers/mermaid_vector/chroma_init.py init

# 5. Optional: warm cache
if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  log "warming OpenRouter cache"
  MODELS_CACHE_PATH=.kilo/models.curated.json \
    uv run scripts/refresh_models.py || warn "refresh failed"
else
  warn "OPENROUTER_API_KEY not set — skipping cache warm-up"
fi

# 6. Tests
log "running tests"
uv run pytest -q || warn "some tests failed — see output above"

cat <<EOF

${GRN}dev bootstrap complete${NC}

  next: edit, then 'uv run pytest' to verify.
  to deploy globally: bash install.sh
EOF
