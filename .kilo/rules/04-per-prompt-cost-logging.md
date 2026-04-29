# Rule 04 — Per-prompt cost logging (per-project)

When the project has been initialized via `project_init.py` (i.e. `./.kilo/project.json` exists), every agent invocation must bracket its work with two snapshot calls so the cost of each prompt can be derived from the OpenRouter `/keys/{hash}.usage` delta.

## The two snapshots

Use the same `<prompt-id>` (the SHA1 hash already used by `sqlite-memory.save_prompt` per Rule 02) for both calls so the report can pair them.

1. **Task start** — immediately after the Rule 02 `save_prompt` open:

   ```bash
   uv run scripts/usage_log.py snapshot \
     --phase start --prompt-id <hash> --agent <agent-slug>
   ```

2. **Task end** — after the work completes and before the Rule 02 `save_prompt` close:

   ```bash
   uv run scripts/usage_log.py snapshot \
     --phase end --prompt-id <hash> --agent <agent-slug>
   ```

Both calls append one JSON line to `./USAGE.log.jsonl`. `usage_report.py` joins start/end pairs and renders `cost = end.key_usage - start.key_usage` per prompt.

## When this rule does NOT apply

- No `./.kilo/project.json` (project wasn't initialized — fall back to account-wide tracking only).
- The `ask` agent running on free models — its OpenRouter cost is always $0, so the snapshots add noise without value. Skip.
- Read-only / dry-run invocations that don't make any model calls.

## Why this matters

The Rule 02 SQLite log captures *what* happened. This rule captures *what it cost*. Together they let the report show "the architect-ch agent spent $0.23 across 4 prompts on this project" — which is the unit of cost the user actually cares about at project completion (`make project-finish`).
