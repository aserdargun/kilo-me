# TODO

Active work tracker for **kilo-me**. Move items to `CHANGELOG.md`'s `[Unreleased]` section once they land; remove them from this file when they ship in a tagged release.

## Current milestone — kilo-zh-stack → kilo-me rename + agent expansion

- [x] Rename project to `kilo-me` (pyproject + every internal reference).
- [x] Remove `kilo-zh` custom command; rely on `kilo auth login` for credential setup.
- [x] Rename agents `*-zh.md` → `*-ch.md` and pin to the agreed model slugs.
  - [x] `architect-ch` → `openrouter/deepseek/deepseek-v4-pro`
  - [x] `coder-ch` → `openrouter/moonshotai/kimi-k2.6`
  - [x] `debugger-ch` → `openrouter/z-ai/glm-5.1`
  - [x] `memory-curator-ch` → `openrouter/qwen/qwen3.6-plus`
  - [x] `cheap-fallback-ch` → `openrouter/deepseek/deepseek-v3.2`
- [x] Add `-us` agent variants (Claude Opus 4.7, GPT-5.4, Sonnet 4.6, Haiku 4.5, Grok 4.1 Fast).
- [x] Add `CHANGELOG.md` at the repo root.
- [x] Reformat `TODO.md` as a checklist (this file).
- [x] Refactor `README.md` and `CLAUDE.md` to reflect new agents, no-wrapper flow, and graph memory.
- [x] Add a graph database (Kuzu) for memory and wire `memory-curator-ch` / `memory-curator-us` to write nodes + edges.
- [x] Make `ask` route through OpenRouter free models only (`top_free_models()` MCP tool + `.kilo/agents/ask.md`).
- [x] Document Windows support (WSL2 / Git Bash) in `README.md` and `CLAUDE.md`.

## Post-merge follow-ups

- [ ] Move the working directory from `kilo-zh-stack/` to `kilo-me/` (`mv ~/Projects/kilo-zh-stack ~/Projects/kilo-me`) and update any local IDE / shell aliases. Out-of-band step — git history follows.
- [ ] Re-run `bash install.sh` after the rename to refresh `~/.config/kilo/`.
- [ ] Re-run `kilo auth login` to confirm the no-wrapper flow works end-to-end.
- [ ] Re-cut the daily cron lines if your crontab pinned them to the old path.

## Nice-to-have / next milestones

- [ ] Native PowerShell installer (`install.ps1`) so Windows users don't need WSL.
- [ ] `graph-memory.delete_node` + cascade delete for pruning bad ingests.
- [ ] CI workflow that runs `make test` + `make lint` across macOS, Linux, and WSL2.
- [ ] Auto-generate `AGENTS.md` from `kilo.jsonc` so the matrix never drifts.
