# =============================================================================
# kilo-me — Makefile
# Run `make help` to see available targets.
# =============================================================================

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

KILO_ME_BASE ?= $(HOME)/.kilo-me
KILO_HOME ?= $(KILO_ME_BASE)/config/kilo
KILO_DATA_HOME ?= $(KILO_ME_BASE)/data/kilo
KILO_STATE_HOME ?= $(KILO_ME_BASE)/state/kilo
AUTH_FILE ?= $(KILO_DATA_HOME)/auth.json
KILO_ME_SHIM ?= $(HOME)/.local/bin/kilo-me

.PHONY: help install-global uninstall uninstall-purge bootstrap dev-deps \
        refresh-models sync-bp usage-report project-init project-finish \
        usage-log-summary test test-mcp lint clean nuke \
        chroma-status chroma-reset memory-status graph-status agent-test where \
        auth-status wrapper-status

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nAvailable targets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ─── Global deployment (most users) ───────────────────────────────────────────

install-global: ## Deploy to ~/.kilo-me/ and drop the kilo-me shim on PATH
	bash install.sh

uninstall: ## Remove kilo-me code+config but keep memory/chroma/graph/decisions/auth.json
	bash uninstall.sh

uninstall-purge: ## Remove ~/.kilo-me/ entirely INCLUDING user data and the shim
	bash uninstall.sh --purge

where: ## Print where things live
	@echo "KILO_ME_BASE    = $(KILO_ME_BASE)"
	@echo "KILO_HOME       = $(KILO_HOME)"
	@echo "KILO_DATA_HOME  = $(KILO_DATA_HOME)"
	@echo "KILO_STATE_HOME = $(KILO_STATE_HOME)"
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
	@echo "  model.json   : $(KILO_STATE_HOME)/model.json"
	@echo "  credentials  : $(AUTH_FILE)"
	@echo "  wrapper      : $(KILO_ME_SHIM)"

wrapper-status: ## Confirm the kilo-me shim exists and is on PATH
	@if [ -x "$(KILO_ME_SHIM)" ]; then \
	  echo "shim: $(KILO_ME_SHIM) (executable)"; \
	else \
	  echo "MISSING: $(KILO_ME_SHIM) — run 'make install-global'"; exit 1; \
	fi
	@case ":$$PATH:" in \
	  *":$$(dirname $(KILO_ME_SHIM)):"*) echo "PATH ok: $$(dirname $(KILO_ME_SHIM)) is on PATH";; \
	  *) echo "WARNING: $$(dirname $(KILO_ME_SHIM)) is NOT on PATH"; \
	     echo "  add this to your shell rc: export PATH=\"$$(dirname $(KILO_ME_SHIM)):\$$PATH\"";; \
	esac
	@command -v kilo-me >/dev/null 2>&1 && echo "resolved: $$(command -v kilo-me)" || echo "kilo-me not resolvable on current PATH"

auth-status: ## Show auth.json location, perms, and which keys it provides
	@uv run mcp_servers/_auth.py 2>/dev/null || \
	  python3 mcp_servers/_auth.py 2>/dev/null || \
	  echo "could not run _auth.py — is python3 installed?"

# ─── Daily ops ────────────────────────────────────────────────────────────────

refresh-models: ## Refresh OpenRouter catalog (uses ~/.kilo-me by default)
	uv run scripts/refresh_models.py

sync-bp: ## Sync promoted patterns to the best-practices repo
	uv run scripts/sync_best_practices.py

usage-report: ## Generate USAGE.md from the OpenRouter API (project-scoped)
	uv run scripts/usage_report.py

project-init: ## Provision a per-project OpenRouter key (writes ./auth.json + ./.kilo/project.json)
	uv run scripts/project_init.py

project-finish: ## Mark project completed; pass DELETE=1 or DISABLE=1 to clean up the key
	@flags=""; \
	if [ "$(DELETE)" = "1" ]; then flags="$$flags --delete-key"; fi; \
	if [ "$(DISABLE)" = "1" ]; then flags="$$flags --disable-key"; fi; \
	uv run scripts/project_finish.py $$flags

usage-log-summary: ## Print top-N completed prompts by cost from USAGE.log.jsonl
	uv run scripts/usage_log.py summary

agent-test: ## End-to-end smoke: ask Kilo (via kilo-me) for the top 3 coding models
	@command -v kilo-me >/dev/null || (echo "kilo-me shim not installed; run 'make install-global'"; exit 1)
	kilo-me run --auto "Use openrouter-models.top_coding_models to list the top 3 Chinese coding models, then write the result as a Mermaid pie chart and ingest it via mermaid-vector.ingest_mermaid."

# ─── Diagnostics ──────────────────────────────────────────────────────────────

memory-status: ## Show SQLite memory size and row count (~/.kilo-me by default)
	@db="$(KILO_HOME)/memory.sqlite"; \
	if [ -f "$$db" ]; then \
	  echo "size: $$(du -h $$db | cut -f1)"; \
	  sqlite3 "$$db" "SELECT 'prompts: ' || COUNT(*) FROM prompts;" 2>/dev/null || true; \
	  sqlite3 "$$db" "SELECT 'promotions: ' || COUNT(*) FROM promotions;" 2>/dev/null || true; \
	else \
	  echo "no memory db at $$db — run 'make install-global'"; \
	fi

chroma-status: ## Show ChromaDB collection stats (~/.kilo-me by default)
	CHROMA_PATH="$(KILO_HOME)/chroma" uv run mcp_servers/mermaid_vector/chroma_init.py status

chroma-reset: ## DESTRUCTIVE: wipe the ChromaDB collection
	CHROMA_PATH="$(KILO_HOME)/chroma" uv run mcp_servers/mermaid_vector/chroma_init.py reset --yes

graph-status: ## Show Kuzu graph node + edge counts per label
	GRAPH_DB_PATH="$(KILO_HOME)/graph.kuzu" uv run --script mcp_servers/graph_memory/server.py --init >/dev/null 2>&1 && \
	  GRAPH_DB_PATH="$(KILO_HOME)/graph.kuzu" uv run --with kuzu --with fastmcp python -c "import importlib.util, json; \
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
