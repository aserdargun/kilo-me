# =============================================================================
# kilo-me — Makefile
# Run `make help` to see available targets.
# =============================================================================

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

XDG_CONFIG_HOME ?= $(HOME)/.config
XDG_DATA_HOME ?= $(HOME)/.local/share
KILO_HOME ?= $(XDG_CONFIG_HOME)/kilo
KILO_DATA_HOME ?= $(XDG_DATA_HOME)/kilo
AUTH_FILE ?= $(KILO_DATA_HOME)/auth.json

.PHONY: help install-global uninstall uninstall-purge bootstrap dev-deps \
        refresh-models sync-bp test test-mcp lint clean nuke \
        chroma-status chroma-reset memory-status graph-status agent-test where \
        auth-status

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nAvailable targets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ─── Global deployment (most users) ───────────────────────────────────────────

install-global: ## Deploy to ~/.config/kilo/ (idempotent; runs install.sh)
	bash install.sh

uninstall: ## Remove ~/.config/kilo code+config but keep memory/chroma/graph/decisions
	bash uninstall.sh

uninstall-purge: ## Remove ~/.config/kilo entirely INCLUDING user data
	bash uninstall.sh --purge

where: ## Print where things live
	@echo "KILO_HOME      = $(KILO_HOME)"
	@echo "KILO_DATA_HOME = $(KILO_DATA_HOME)"
	@echo ""
	@echo "  config       : $(KILO_HOME)/kilo.jsonc"
	@echo "  agents       : $(KILO_HOME)/agents/"
	@echo "  rules        : $(KILO_HOME)/rules/"
	@echo "  mcp servers  : $(KILO_HOME)/mcp_servers/"
	@echo "  memory db    : $(KILO_HOME)/memory.sqlite"
	@echo "  chroma       : $(KILO_HOME)/chroma/"
	@echo "  graph db     : $(KILO_HOME)/graph.kuzu/"
	@echo "  model cache  : $(KILO_HOME)/models.curated.json"
	@echo "  decisions    : $(KILO_HOME)/decisions/"
	@echo "  credentials  : $(AUTH_FILE)  (run 'kilo auth login' to populate)"

auth-status: ## Show auth.json location, perms, and which keys it provides
	@uv run mcp_servers/_auth.py 2>/dev/null || \
	  python3 mcp_servers/_auth.py 2>/dev/null || \
	  echo "could not run _auth.py — is python3 installed?"

# ─── Daily ops ────────────────────────────────────────────────────────────────

refresh-models: ## Refresh OpenRouter catalog (uses ~/.config/kilo by default)
	uv run scripts/refresh_models.py

sync-bp: ## Sync promoted patterns to the best-practices repo
	uv run scripts/sync_best_practices.py

agent-test: ## End-to-end smoke: ask Kilo for the top 3 coding models
	@command -v kilo >/dev/null || (echo "kilo CLI not installed; npm i -g @kilocode/cli"; exit 1)
	kilo run --auto "Use openrouter-models.top_coding_models to list the top 3 Chinese coding models, then write the result as a Mermaid pie chart and ingest it via mermaid-vector.ingest_mermaid."

# ─── Diagnostics ──────────────────────────────────────────────────────────────

memory-status: ## Show SQLite memory size and row count (~/.config/kilo by default)
	@db="$(KILO_HOME)/memory.sqlite"; \
	if [ -f "$$db" ]; then \
	  echo "size: $$(du -h $$db | cut -f1)"; \
	  sqlite3 "$$db" "SELECT 'prompts: ' || COUNT(*) FROM prompts;" 2>/dev/null || true; \
	  sqlite3 "$$db" "SELECT 'promotions: ' || COUNT(*) FROM promotions;" 2>/dev/null || true; \
	else \
	  echo "no memory db at $$db — run 'make install-global'"; \
	fi

chroma-status: ## Show ChromaDB collection stats (~/.config/kilo by default)
	CHROMA_PATH="$(KILO_HOME)/chroma" uv run mcp_servers/mermaid_vector/chroma_init.py status

chroma-reset: ## DESTRUCTIVE: wipe the ChromaDB collection
	CHROMA_PATH="$(KILO_HOME)/chroma" uv run mcp_servers/mermaid_vector/chroma_init.py reset --yes

graph-status: ## Show Kuzu graph node + edge counts per label
	GRAPH_DB_PATH="$(KILO_HOME)/graph.kuzu" uv run --script mcp_servers/graph_memory/server.py --init >/dev/null 2>&1 && \
	  uv run python -c "import importlib.util, json; \
	  spec = importlib.util.spec_from_file_location('g', 'mcp_servers/graph_memory/server.py'); \
	  mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); \
	  print(json.dumps(mod.stats(), indent=2))"

# ─── Dev workflow (working on the stack itself) ──────────────────────────────

bootstrap: ## DEV: project-local setup (venv + project-scoped .kilo/)
	bash scripts/bootstrap.sh

dev-deps: ## DEV: install/refresh dev deps via uv
	uv pip install -e ".[dev]"

test: ## DEV: run the full test suite
	uv run pytest

test-mcp: ## DEV: test only MCP servers (fast)
	uv run pytest mcp_servers/

lint: ## DEV: type-check + lint
	uv run mypy --strict mcp_servers/ scripts/ || true
	uv run ruff check mcp_servers/ scripts/ || true

# ─── Cleanup ──────────────────────────────────────────────────────────────────

clean: ## Remove caches and pycache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -prune -exec rm -rf {} +
	find . -type d -name ".ruff_cache" -prune -exec rm -rf {} +

nuke: clean ## DESTRUCTIVE: also wipe project-local .kilo/ and .venv
	@read -p "Really wipe .kilo/ and .venv/? [y/N] " ans && [ "$$ans" = "y" ] || exit 1
	rm -rf .kilo .venv
