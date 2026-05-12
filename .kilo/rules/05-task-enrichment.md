# Rule 05 — Pre-flight task enrichment (gpt-5.4)

Before doing real work on any task, agents in scope must call `task-enricher.enrich_task` and incorporate the returned brief into their plan. This catches ambiguous prompts, surfaces foot-guns, and pre-narrows the file set the agent will read — paying for itself in fewer wasted tool calls.

## Agents in scope

Required for: `architect-ch`, `architect-us`, `coder-ch`, `coder-us`.

Optional for: `debugger-ch`, `debugger-us` (call only when the bug report is vague — a clear stack trace doesn't need enrichment).

Skipped for: `ask` (free-models lane, never enriched), `memory-curator-*` (post-task curator), `cheap-fallback-*` (intentionally minimal), `accountant-*` (deterministic reporting).

## The call

At the very start of the task, before any other tool call (including the Rule 02 `save_prompt` open):

```text
task-enricher.enrich_task(
  prompt = <verbatim user request>,
  agent  = "<your agent slug>",
  context = <optional: 1-3 sentence repo / project summary>,
)
```

The tool returns a JSON object:

```json
{
  "objectives":     ["..."],
  "constraints":    ["..."],
  "files_likely":   ["..."],
  "risks":          ["..."],
  "open_questions": ["..."],
  "summary":        "...",
  "_enriched":      true
}
```

## How to use the brief

1. **Open questions first.** If `open_questions` is non-empty, ask the user before touching code. Do not guess. Append the chosen answers to the prompt before continuing.
2. **Plan against `objectives` + `constraints`.** Use them as acceptance criteria. If the user's request conflicts with a constraint, surface it.
3. **Seed reads from `files_likely`.** Use it as the first batch passed to `Read`/`Grep`. Treat it as a hint, not an oracle — verify each file exists before reading.
4. **Mention `risks` in your final summary** when any of them materialized. This feeds Rule 02's outcome log honestly.

## When the brief is empty / failed

The tool returns `_enriched: false` when:
- `KILO_ENRICH=0` is set in the environment (operator opt-out).
- Neither `OPENAI_API_KEY` nor `OPENROUTER_API_KEY` is configured.
- The OpenAI / OpenRouter call failed (timeout, rate-limit, 5xx).

In all of these cases, **proceed with the task normally** — enrichment is best-effort, never a blocker. Note the failure mode in your task log so the Memory Curator can flag a misconfiguration.

## Opting out

- **Per-machine:** set `KILO_ENRICH=0` in your shell rc or in `~/.kilo-me/data/kilo/auth.json` (the loader exports it to env).
- **Per-project:** put `KILO_ENRICH=0` in `./.kilo/project.json`'s env section, or in a project-local `.env`.

## Why gpt-5.4 specifically

The default model (`TASK_ENRICH_MODEL=gpt-5.4`) is chosen because most users already pay for an OpenAI subscription — the enrichment pass piggy-backs on that and costs nothing extra on the metered OpenRouter side. Users without an OpenAI key fall back automatically to `openrouter/openai/gpt-5.4` (metered, but cheap at the 1K-token max).
