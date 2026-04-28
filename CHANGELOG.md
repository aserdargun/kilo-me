# Changelog

All notable changes to **kilo-me** are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `-us` agent lineup (`architect-us`, `coder-us`, `debugger-us`, `memory-curator-us`, `cheap-fallback-us`) reachable by full name; built-in slots remain on the `-ch` defaults.
- `cheap-fallback-ch.md` and `ask.md` agent prompts (previously `cheap-fallback` lived only in `kilo.jsonc`; there was no `ask` agent at all).
- `graph-memory` MCP server backed by embedded Kuzu — node tables for `Prompt`, `Agent`, `Tag`, `Diagram`, `Decision`, `Pattern`; rel tables for `LOGGED_BY`, `TAGGED`, `DEPICTS`, `PROMOTED_TO`, `DERIVES`. Memory Curator agents write to it after every task.
- `openrouter-models.top_free_models()` MCP tool — filters cached models with zero pricing or `:free` suffix; the `ask` agent calls it at task start so paid tokens are never spent on Q&A.
- `make graph-status` target (Kuzu node + edge counts per label).
- `CHANGELOG.md` (this file) and a checklist-form `TODO.md` at the repo root.
- Coder agent rule 9 now mandates `CHANGELOG.md` + `TODO.md` (alongside the existing `README.md` + `AGENTS.md` mandate) for every newly scaffolded project.
- Windows usage section in `README.md` covering WSL2 / Git Bash setup.
- `kuzu>=0.4.0` in `[project.optional-dependencies] dev` for tests.

### Changed
- **Project renamed** `kilo-zh-stack` → `kilo-me`; `pyproject.toml` `name`, version bumped to `0.3.0`, and every README/CLAUDE/agent-prompt/script reference updated.
- Agent files renamed `*-zh.md` → `*-ch.md`. `memory-curator.md` → `memory-curator-ch.md`.
- `architect-ch.md` frontmatter `model:` aligned to `openrouter/deepseek/deepseek-v4-pro` (previously diverged from `kilo.jsonc`).
- `cheap-fallback-ch` model swapped from `deepseek/deepseek-v4-flash` → `deepseek/deepseek-v3.2`.
- `kilo.jsonc` now declares 11 agents (4 built-in slots + 5 `-ch` + 5 `-us`) and 4 MCP servers (the new `graph-memory` joins the existing three).
- `install.sh` no longer copies the `kilo-zh` wrapper, no longer bootstraps `auth.json` from `$OPENROUTER_API_KEY`, and no longer rewrites `$PATH`. Authentication is now handled by Kilo's own `kilo auth login`. The model.json patch keys are updated to match the new agent names. A new step initializes the Kuzu graph store.
- `uninstall.sh --purge` also removes `graph.kuzu/`.
- `Makefile` has lost the wrapper target and gained `graph-status`.

### Removed
- `scripts/kilo-zh` bash wrapper (deleted; no longer needed now that `kilo auth login` writes `auth.json` directly).
- The pre-existing `bin/kilo-zh` PATH plumbing in `install.sh`.

## [0.2.0] — pre-rename baseline

The last `kilo-zh-stack`-named release. Highlights from `git log`:
- `feat: update models and permissions for architect and coder agents; enhance install script and documentation` (f561f4d)
- `feat(install): add plan model configuration to state initialization` (498284e)
- `feat: update installation script and server to pin specific models` (bb2c4f3)
- `feat(auth): introduce native provider-based auth format` (2cd9583)
- `refactor(agents): update model references to OpenRouter prefixes and latest versions` (9a6e669)
- `chore: add initial project files and structure` (9dc2639)

[Unreleased]: https://github.com/aserdargun/kilo-me/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/aserdargun/kilo-me/releases/tag/v0.2.0
