# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`kilo-me` is a globally-installed configuration bundle for the [Kilo Code](https://kilo.ai) CLI. It is **not** a Python package you import from — it is a set of self-contained scripts plus templated config that `install.sh` deploys to `~/.config/kilo/` (XDG Base Directory spec) so Kilo behaves the same way across every project on the machine. Credentials live separately at `~/.local/share/kilo/auth.json` (chmod 600), populated by Kilo's own `kilo auth login` flow.

Beyond the config bundle, the repo also ships a **per-project cost-tracking workflow**: an OpenRouter management/provisioning key in the global `auth.json` is used by `scripts/project_init.py` to mint a dedicated sub-key per project, agents log per-prompt cost deltas via `scripts/usage_log.py`, and `scripts/usage_report.py` renders a project-scoped `USAGE.md` with a final-cost banner once `scripts/project_finish.py` runs. See [§ Per-project cost tracking](#per-project-cost-tracking) below.

Two operating modes:

- **Global install** (end-user): `install.sh` copies `.kilo/{agents,rules}/`, `mcp_servers/`, `scripts/` to `$KILO_HOME` (= `~/.config/kilo/`), renders `kilo.jsonc` with absolute paths, initializes the SQLite/Chroma/Kuzu stores, and pre-warms uv's cache. Authentication is a separate step (`kilo auth login`) — the installer no longer bootstraps `auth.json` from environment variables.
- **Project-local dev** (working on the stack itself): `scripts/bootstrap.sh` creates a `.venv/`, installs `[dev]` extras, and points all stores at `./.kilo/` instead of `~/.config/kilo/`.

The `.kilo/` directory in this repo is the **template** that gets copied on install — it is also used as the project-local store during dev. Don't conflate the two: edits to `.kilo/kilo.jsonc` here are templates (with `__KILO_HOME__` placeholders); the live config is at `~/.config/kilo/kilo.jsonc`.

Windows runs via WSL2 / Git Bash; no native PowerShell installer yet.

## Common commands

Most work uses `make` targets (run `make help` for the full list):

```bash
# Dev workflow
make bootstrap         # one-time: venv + project-local .kilo/ stores + tests
make test              # uv run pytest (whole suite)
make test-mcp          # MCP-server tests only (faster)
make lint              # mypy --strict + ruff (both non-fatal: `|| true`)

# Single-test invocation
uv run pytest mcp_servers/sqlite_memory/test_sqlite_memory.py -k test_name -v
uv run pytest mcp_servers/graph_memory/test_graph_memory.py -v
uv run pytest tests/test_smoke.py -v           # the cross-cutting e2e

# Global install / ops
bash install.sh                                # idempotent global deploy
kilo auth login                                # populate auth.json (post-install)
make refresh-models                            # force-refresh OpenRouter cache
make sync-bp                                   # push promoted patterns to GitHub
make where                                     # print all paths (incl. auth.json)
make auth-status                               # which keys auth.json provides
make memory-status / make chroma-status / make graph-status   # diagnostics

# Per-project cost tracking (run from inside a project directory)
make project-init                              # provision per-project sub-key (uses admin key)
make usage-report                              # render ./USAGE.md
make usage-log-summary                         # top-N expensive prompts from USAGE.log.jsonl
make project-finish [DISABLE=1|DELETE=1]       # snapshot final cost; optionally clean up key
```

The dev test suite assumes `uv` is on PATH — `install.sh`/`bootstrap.sh` install it via `astral.sh` if missing. Tests do not require an `OPENROUTER_API_KEY`; they monkeypatch `_fetch_models`.

## Architecture: the four load-bearing pieces

### 1. PEP 723 inline scripts + `uv run --script`

Every MCP server and helper script begins with an inline metadata block:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["fastmcp>=0.4.0", ...]
# ///
```

`pyproject.toml`'s `[project] dependencies` is **intentionally empty**. Runtime deps live exclusively in each script's PEP 723 block, and `kilo.jsonc` invokes them via `["uv", "run", "--script", "<path>"]`. This means each MCP server's deps are isolated (an upgrade to one can't break another), and there is no shared venv to keep in sync. The `[project.optional-dependencies] dev` group exists *only* for the dev workflow (test discovery, mypy, ruff).

When adding a new script that needs deps: put them in its own PEP 723 block, not in `pyproject.toml`. Mirror them in `[project.optional-dependencies] dev` only if tests need them.

### 2. Four local stdio MCP servers, all XDG-rooted

Each server in `mcp_servers/<name>/server.py` defaults its data path to `$XDG_CONFIG_HOME/kilo/<store>` (overridable via env vars). These overrides are how dev mode redirects to `./.kilo/`:

| Server | Default store | Override env var | Purpose |
|---|---|---|---|
| `sqlite-memory` | `~/.config/kilo/memory.sqlite` | `MEMORY_DB_PATH` | Prompt/outcome log w/ FTS5 search; idempotent on `(agent, prompt, day)` |
| `openrouter-models` | `~/.config/kilo/models.curated.json` | `MODELS_CACHE_PATH` | Auto-ranked OpenRouter catalog (24h TTL); also exposes `top_free_models` for the `ask` agent |
| `mermaid-vector` | `~/.config/kilo/chroma/` | `CHROMA_PATH` | ChromaDB embeddings of architectural Mermaid diagrams |
| `graph-memory` | `~/.config/kilo/graph.kuzu/` | `GRAPH_DB_PATH` | Kuzu graph linking prompts ↔ agents ↔ tags ↔ diagrams ↔ decisions ↔ patterns |

`mcp_servers/_auth.py` is the **single source of truth** for credentials. It reads `auth.json` (whatever `kilo auth login` wrote) and populates `os.environ` *without* overriding existing env vars (shell wins). `openrouter_models/server.py` imports it at startup via `importlib.util.spec_from_file_location` (file-spec import, not package import) — this keeps each server self-contained for `uv run --script` while still sharing the loader. If you add a new script that needs `OPENROUTER_API_KEY`, follow the same `_load_auth_json()` pattern; don't hand-roll JSON parsing. `graph-memory` and `sqlite-memory` don't need OpenRouter auth and intentionally skip the import.

The cross-server contract: `sqlite-memory.save_prompt` returns a prompt ID; `mermaid-vector.ingest_mermaid` returns a diagram ID; the prompt row links to the diagram via `mermaid_id`. The Memory Curator agent then mirrors that record into the graph: `graph-memory.add_node("Prompt", prompt_id, …)`, plus edges to `Tag`, `Diagram`, and (when promotion fires) `Decision` nodes. The smoke test (`tests/test_smoke.py`) exercises the full handoff including the graph write.

### 3. The promotion pipeline (the "self-improving" part)

Patterns graduate from local SQLite to a separate GitHub `kilo-best-practices` repo when **all** of:
1. Same primary domain tag in **3+** rows
2. All have `success = true`
3. Span **2+ distinct days**
4. At least one has a Mermaid diagram in `mermaid-vector`

`sqlite-memory.count_pattern` returns `promotion_ready: bool` based on these criteria. The Memory Curator agent (`.kilo/agents/memory-curator-ch.md` or `-us.md`) writes `docs/decisions/<kebab-title>.md` AND adds a `Decision` node to the graph linked to every contributing `Prompt` via `PROMOTED_TO`; `make sync-bp` (or the cron in `scripts/sync_best_practices.py`) opens a PR against `BP_REPO_PATH`. Anti-patterns deliberately *not* promoted: one-off bug fixes, single-success patterns, anything done in `--auto` without human review (see `.kilo/rules/03-best-practice-promotion.md`).

### 4. Per-project cost tracking

Cost is tracked **per project**, not per account. Each project gets its own OpenRouter sub-key, every prompt's cost is captured as a `/keys/{hash}.usage` delta, and the project's `USAGE.md` is the running ledger. Lifecycle:

1. **`scripts/project_init.py`** — calls `POST /api/v1/keys` with the admin key (read from `OPENROUTER_ADMIN_KEY` in the global `auth.json`), creates a sub-key labeled after the project, and writes:
   - `./auth.json` (chmod 600) with `{"openrouter": {"key": "..."}, "OPENROUTER_KEY_HASH": "..."}` — agents/Kilo pick this up via the per-project resolver in `usage_report.py`
   - `./.kilo/project.json` with `{name, key_hash, key_label, key_limit, status, created_at, finished_at}`
2. **`scripts/usage_log.py snapshot --phase {start,end} --prompt-id <id>`** — agents call this around each prompt (per Rule 04). Each call hits `GET /keys/{hash}` and appends one JSON line to `./USAGE.log.jsonl`. The cost of a single prompt is `end.key_usage − start.key_usage` (paired by `prompt_id`).
3. **`scripts/usage_report.py`** — reads `.kilo/project.json`; if a key hash is present, prefers admin `/keys/{hash}` for authoritative spend (more accurate than `/key`). Renders `USAGE.md` with sections: project metadata, project key spend, cost trend (from `USAGE.history.jsonl`, appended each run), per-prompt costs (by-agent rollup + top 10), and the optional account-wide `/activity` breakdown.
4. **`scripts/project_finish.py`** — marks `.kilo/project.json` `status="completed"`, snapshots the final `/keys/{hash}.usage` into `final_usage`, and on `--disable-key` / `--delete-key` patches or deletes the OpenRouter key. Subsequent `usage-report` runs render a "Project completed / Final cost: $X.XX" banner at the top.

Two-key model: the admin key NEVER leaves `~/.local/share/kilo/auth.json`; projects only ever see their own scoped sub-key. Both `auth.json` (per-project) and the admin key are read by the same shared `_auth.py` loader.

Files written into each project (all gitignored except `USAGE.md`):

| Path | Written by | Notes |
|---|---|---|
| `./auth.json` | `project_init.py` | chmod 600; per-project sub-key |
| `./.kilo/project.json` | `project_init.py` / `project_finish.py` | project state + key hash |
| `./USAGE.md` | `usage_report.py` | the ledger — committed |
| `./USAGE.history.jsonl` | every `usage_report.py` run | per-machine snapshot trail |
| `./USAGE.log.jsonl` | every `usage_log.py snapshot` | per-prompt cost log |

Why this design rather than per-prompt OpenAI-style usage objects:
- `/key` only sees the calling key, not other keys → can't attribute per-project spend
- `/activity` is account-wide and exposes no `X-Title` tag → can't filter by project
- `POST /generation` requires scraping `id` from each chat-completion response → would need a proxy
- Polling `/keys/{hash}.usage` before/after each prompt is a single fast HTTP call, key-scoped, and works with whatever Kilo runtime ships

## Project conventions (from `.kilo/rules/` and agent prompts)

These rules govern **the agents Kilo runs**, not Claude Code itself, but they shape how this codebase is structured:

- **Mermaid required** for any change introducing a module/service, altering data flow between components, or modifying the agent matrix / MCP roster (Rule 01). Pure refactors and dep bumps are exempt. Diagrams are ingested via `mermaid-vector.ingest_mermaid` with `%% title:` and `%% tags:` comment headers.
- **Two-write logging** per task: `sqlite-memory.save_prompt` is called once at task start with `success=null` and once at completion with the same prompt content (idempotent on the SHA1 of `agent|day|prompt`) (Rule 02). The Memory Curator additionally writes a `Prompt` node and edges to the graph after every successful task.
- **Coder file-perm boundaries** (`.kilo/agents/coder-ch.md`, `coder-us.md`): the Coder agents may edit `src/`, `tests/`, `mcp_servers/`, `scripts/`, `docs/`. Architect cannot edit. Memory Curator can only edit `docs/decisions/`. Debugger has no edit perms — it emits diff suggestions in a structured report.
- **Coder scaffold mandate**: when a Coder agent generates a *new* project, it always creates `README.md`, `AGENTS.md`, `CHANGELOG.md`, and `TODO.md`. Don't skip any of these even on small scaffolds — the rest of the agent fleet expects them to exist.
- **No direct OpenRouter calls** from agent code — model selection always routes through `openrouter-models.model_for_budget` so choices are auditable. The `ask` agent additionally routes through `openrouter-models.top_free_models()` so it never spends paid tokens.
- **Two lanes, one default**: built-in slots (`architect`, `code`, `debug`, `ask`) point at the `-ch` lineup. The `-us` lineup is reachable by full name (`@coder-us`, `@architect-us`, etc.) and intentionally *not* the default.
- **Per-prompt cost snapshots** when `./.kilo/project.json` exists (Rule 04): every agent invocation brackets its work with `usage_log.py snapshot --phase start` and `--phase end` using the same `prompt-id` as the Rule 02 SQLite log. The `ask` agent (always free) and read-only invocations are exempt.

## Where things live (cheatsheet)

```
kilo-me/                                # repo root
├── .kilo/                              # TEMPLATE — installer copies most of this to $KILO_HOME
│   ├── kilo.jsonc                      # has __KILO_HOME__ placeholders; rendered on install
│   ├── agents/{architect,coder,debugger,memory-curator,cheap-fallback}-{ch,us}.md  # 10 agents
│   ├── agents/ask.md                   # free-models-only Q&A agent
│   └── rules/{01,02,03,04}-*.md        # prompt-injected behavior rules (04 = per-prompt cost log)
├── mcp_servers/
│   ├── _auth.py                        # shared auth.json loader (single source of truth)
│   ├── test_auth.py                    # 11 cases for the loader
│   ├── sqlite_memory/                  # FTS5-backed prompt log; schema.sql holds DDL
│   ├── openrouter_models/              # imports _auth.py at startup
│   ├── mermaid_vector/                 # ChromaDB; chroma_init.py is the init/reset CLI
│   └── graph_memory/                   # Kuzu graph; schema.cypher mirrors the in-server DDL
├── scripts/
│   ├── bootstrap.sh                    # DEV: project-local setup
│   ├── refresh_models.py               # PEP 723 cron entry
│   ├── sync_best_practices.py          # PEP 723 git push to BP repo
│   ├── project_init.py                 # provision per-project OpenRouter sub-key
│   ├── usage_log.py                    # per-prompt cost snapshots → USAGE.log.jsonl
│   ├── usage_report.py                 # render USAGE.md (project + cost trend + per-prompt)
│   └── project_finish.py               # snapshot final cost; optionally disable/delete key
├── tests/test_smoke.py                 # cross-cutting e2e — uses load_mcp_server fixture
├── CHANGELOG.md                        # release log
├── TODO.md                             # active work tracker (checklist)
└── pyproject.toml                      # dev-only deps in [project.optional-dependencies] dev
```

After a global install, the runtime layout is at `~/.config/kilo/` (code+config+state) and `~/.local/share/kilo/auth.json` (credentials only).

## Testing notes

`tests/conftest.py` provides a `load_mcp_server` fixture that uses `importlib.util.spec_from_file_location` to load each server under a unique module name (`_mcp_<dirname>`). All four servers are named `server.py`, so a normal package import collides — always use the fixture in cross-server tests. The fixture also pops cached modules between tests so `monkeypatch.setenv` of `MEMORY_DB_PATH` / `GRAPH_DB_PATH` etc. takes effect on re-import.

`tests/test_smoke.py` is the canonical example of wiring all four MCP servers together against `tmp_path` — it exercises `sqlite-memory.save_prompt` → `mermaid-vector.ingest_mermaid` → `graph-memory.add_node` + `add_edge` and asserts the relationships land where the Memory Curator agent expects them.
