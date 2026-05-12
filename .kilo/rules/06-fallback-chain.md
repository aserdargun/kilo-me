# Rule 06 — Model fallback chain (resilience under hard failure)

Every agent has a configured 5-model fallback chain in `$KILO_HOME/fallbacks.json` (a copy of `.kilo/fallbacks.json`, editable per-machine). When the primary model fails for a recoverable reason, walk the chain instead of bubbling the error.

## When this rule fires

Trigger fallback on any of:

| Failure                          | How you'll see it                                                |
| -------------------------------- | ---------------------------------------------------------------- |
| Rate limit                       | HTTP `429`, or "rate limit exceeded" in the body                 |
| Server / provider error          | HTTP `5xx`, gateway timeout, "model is down"                     |
| Out of credits                   | HTTP `402`, "insufficient credits", "key limit exceeded"         |
| Network timeout                  | `httpx.TimeoutException`, request hung past the configured limit |
| Tool-call malformed / unsupported| Provider responds but tool-calling failed at the protocol level  |

**Do NOT trigger fallback for:**
- A `4xx` other than `429` / `402` — that's a bug in the request; switching models won't fix it.
- A model returning *bad output* (wrong code, hallucination). Fallback is a transport-level remedy, not a quality knob.
- Anything inside a user-cancelled task. Honor the cancel.

## The call

When you detect a triggering failure:

```text
openrouter-models.pick_fallback(
  agent = "<your agent slug>",      # e.g. "coder-ch", "architect-us"
  exclude = [<every slug already tried this turn, INCLUDING the primary>],
  skip_unavailable = true,           # default; skips slugs not in the curated cache
)
```

The tool returns:

```json
{
  "model":     "openrouter/deepseek/deepseek-v4-pro",
  "remaining": 3,
  "tried":     ["openrouter/moonshotai/kimi-k2.6"],
  "chain":     [...],
  "reason":    "ok"
}
```

Branch on `reason`:

- `"ok"` — switch the active model to `model` (Kilo command `/model <slug>`) and retry the **same** prompt **once**. Do not re-enrich (Rule 05) on retry. Do not increment the Rule 02 prompt id.
- `"exhausted"` — every model in the chain has been tried. Surface the error to the user with the original failure body and the list of attempts: "Tried 5 models, all failed: [chain]". Stop.
- `"no-chain"` — no fallback configured for this agent slug. Surface the error normally; flag this to the Memory Curator so the operator can add a chain.

## Retry budget

- One retry per fallback model. If the fallback also fails, call `pick_fallback` again with the expanded `exclude` list.
- Maximum 5 attempts total per prompt (primary + 4 fallbacks). The chain is already capped at 5 entries.
- Between attempts, sleep `min(2 ** attempt, 30)` seconds to avoid hammering a degraded provider.
- Record every attempt in the Rule 02 SQLite log as a separate `fallback_attempt` row (same `prompt_id`, `attempt_n` counter, `failure_kind` tag). The Memory Curator depends on this to spot chronically flaky models.

## Cost accounting

Each fallback attempt **does** incur cost on its own model. The Rule 04 snapshots already capture this correctly because they bracket the *entire* task, not individual attempts — so the `key_usage` delta naturally includes all retries. No additional logging needed for cost; only for diagnostics (see "Retry budget" above).

## Editing the chain

`fallbacks.json` is plain JSON. Re-running `install.sh` preserves user edits (the installer only writes the file if it doesn't already exist). To regenerate from template, delete the file first.

When adding a new agent: append a `"<slug>": ["...", "...", "...", "...", "..."]` entry. The `pick_fallback` tool reads the file on every call — no restart required.

## Why the chain is 5 long

Five attempts catches ~all transient outages (most clear within 60s) without burying genuine errors under a wall of retries. Empirically, anything past 5 indicates a systemic issue (account-level rate limit, expired key, regional outage) where the right move is to surface the failure, not keep retrying.
