# Rule 02 — Every task is logged to SQLite

Every agent invocation in kilo-me records two SQLite writes:

1. **Task start.** Call `sqlite-memory.save_prompt` with `success=null` (open) before doing significant work. This creates the row early so crashes still leave a trail.

2. **Task end.** Update the same row by calling `sqlite-memory.save_prompt` again with the same prompt hash and a final `success` boolean, completion text, and token counts.

## Required fields

| Field | Source | Notes |
|---|---|---|
| `agent` | The agent slug invoking the call | `coder-ch`, `architect-ch`, `debugger-ch`, `memory-curator-ch` (or their `-us` peers) |
| `model` | The OpenRouter slug actually used | e.g., `moonshotai/kimi-k2.6` |
| `prompt` | The user's task verbatim | Truncate to 8000 chars if longer |
| `completion` | Final agent output, last 4000 chars | Empty on task start |
| `tokens_in` / `tokens_out` | From OpenRouter usage object | 0 on task start |
| `success` | bool or null | null = in-flight |
| `tags` | string array | See taxonomy in agents/memory-curator-ch.md |
| `mermaid_id` | string | Set after `mermaid-vector.ingest_mermaid` returns |

## Why this matters

The SQLite store is the substrate the memory-curator uses to detect repeated patterns and promote them to the GitHub best-practices repo. Skipping a log breaks the promotion pipeline.
