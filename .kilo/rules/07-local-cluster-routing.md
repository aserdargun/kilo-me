# Rule 07 — Local cluster routing (Tailscale + Ollama)

`-lo` agents and the built-in slots (`architect`, `code`, `debug`) are pinned to the local cluster — a Tailscale-joined set of Ollama workers under the user's control. Cloud variants (`-ch`, `-us`) remain reachable by full name and act as Rule 06 fallback targets when the cluster is unavailable.

## Topology

| Tier      | Node             | Hardware                | Role                               |
| --------- | ---------------- | ----------------------- | ---------------------------------- |
| Router    | Pi 5 8 GB        | (Phase 3+)              | Classifier + proxy on port 8080    |
| Soft tier | Mac mini M4 24GB | Apple Silicon, Metal    | 4–14 B q4/q5 models                |
| Hard tier | RTX 4070 Ti 16GB | CUDA, 16 GB VRAM        | 14–22 B q5/q8 models               |

Two deployment modes are supported:

**Router mode (Phase 3, recommended)** — both `local` and `local-hard` providers in `auth.json` point at the Pi router (`http://pi:8080/v1`). The router classifies each request by model slug, prompt size, and tool-array length, then forwards to the right worker. If the chosen tier is unhealthy the router fails over to the other tier inline; if both are dead it returns `503`, which trips the client's Rule 06 fallback to cloud.

**Direct mode (Phase 2 fallback)** — without the Pi, the `local` provider points at the Mac mini and `local-hard` points at the RTX. Routing is static per agent: any `-lo` agent's `model:` slug encodes its tier via the provider prefix. Rule 06 chains still bridge tiers and cloud.

The Phase 2 dual-provider config also works under router mode — both `local-hard/<model>` and `local/<model>` requests reach the Pi router and the model field is what the router actually classifies on.

## Routing rules (router-mode, applied per request)

1. Model present on exactly one tier → that tier.
2. Model on multiple tiers + estimated tokens > 8 K → hard.
3. Model on multiple tiers + `tools` array length > 5 → hard.
4. Model on multiple tiers + otherwise → **(Phase 5)** if `router_assist.enabled`, the 1B model on the Pi gets one shot to call it; on timeout / parse-error / vote-for-unknown-tier we fall through to rule 5.
5. Same case as 4, no assist or assist failed → first tier alphabetically (deterministic; "hard" beats "soft").
6. Model unknown → `routing.default_tier` from `config.yaml` (default: `soft`).

Direct mode hard-codes only rule 1 via the provider prefix; rules 2–6 require the router.

### Router-assist (Phase 5)

The assist is a 1B-class instruct model (default `qwen2.5:1.5b-instruct-q4_K_M`) running on the Pi alongside the router. It is asked one binary question per ambiguous request — "is this a small/fast or large/capable task?" — and votes via a single digit reply (`1` → soft, `2` → hard). The default 1.5 s timeout keeps latency bounded; on any failure the static rule-5 answer is used and the request still goes out.

The assist is **off by default**. Enable it by re-running `setup-pi.sh` with `ENABLE_ASSIST=1`, or by flipping `router_assist.enabled` in `/etc/kilo-router/config.yaml` (after manually pulling the model). Disabling is symmetric: set `enabled: false` and `systemctl reload kilo-router`.

Every assist decision is logged with reason `assist LLM voted (<latency>; raw=<first 50 chars>)` so operators can audit whether the LLM is actually adding value vs noise.

## Where the wire goes

- Agents send OpenAI-compatible `/v1/chat/completions` requests to `auth.json.local.base_url` (and `auth.json["local-hard"].base_url`).
- In **router mode** both URLs are the Pi (`http://pi:8080/v1`); the router strips the client's `Authorization` header before forwarding to Ollama and adds `X-Kilo-Tier: <soft|hard>` to the response.
- In **direct mode** each URL is a worker's Ollama endpoint directly; Ollama natively serves `/v1` so no translation is needed.
- All traffic rides Tailscale — never the public LAN. Each worker's `OLLAMA_HOST` is bound to its Tailscale IP only, so even on the same Wi-Fi a non-tailnet device can't reach it.

## Tool-calling discipline

This rule is **strict**. The only models in the `-lo` fallback chains are those with verified OpenAI-format tool-calling under Ollama:

| Model                              | Tool-calling     | Notes                                |
| ---------------------------------- | ---------------- | ------------------------------------ |
| `qwen3-coder:14b-instruct-q8_0`    | ✓ native         | Coder primary (hard)                 |
| `qwen3-coder:7b-instruct-q5_K_M`   | ✓ native         | Coder primary (soft)                 |
| `devstral:22b-q5_K_M`              | ✓ native         | Architect primary                    |
| `deepseek-r1-distill-qwen:14b`     | ✓ via parser     | Debugger primary; reasoning prefix   |
| `llama3.3:8b-instruct-q5_K_M`      | ✓ native         | Memory-curator + accountant          |
| `phi-4:14b-q4_K_M`                 | ✓ native         | Debugger soft alt                    |
| `gemma3:4b`                        | ⚠ best-effort    | Cheap-fallback only                  |

Do not extend the `-lo` chains with models outside this table without verifying tool-calling works end-to-end (the smoke test in `setup-macmini.sh` is the reference).

## Failure handling

`-lo` agents inherit Rule 06 — on `429` / `5xx` / timeout / 402 from the worker, walk the fallback chain. Every `-lo` chain ends with cloud models so a worker outage degrades to "more expensive cloud call," not "task fails."

Specifically:
- **Worker unreachable** (Tailscale down, daemon crashed): `httpx.ConnectError` → Rule 06 kicks in immediately.
- **Worker overloaded** (OOM, model failed to load): Ollama returns `500` with `{"error": "..."}`. Treat as `5xx`.
- **Cold start in progress**: Ollama returns `200` but takes 30–60 s for the first token. **Do not** treat this as failure; it's normal post-reboot behavior. Configure the client timeout to ≥ 90 s.
- **Tool-call malformed**: model returned `200` but no tool_calls array when one was required. Rule 06 fires; the next model in the chain gets the same prompt.

## Privacy posture

A task that starts and finishes on `-lo` agents leaves **no trace** outside the user's tailnet:
- Prompts and completions: stay on the worker.
- Tool call payloads (file reads, shell outputs): never leave the dev machine in a remote API call.
- Memory writes (SQLite, Chroma, Kuzu): already local-only by design.

Cost accounting (`USAGE.md`) records the local task with `key_usage` ≈ 0, but **duration** is still meaningful — see `accountant-lo.md` for the disclosure pattern.

If the fallback chain escalates to cloud, the prompt does leave the tailnet. The accountant agent flags this in `USAGE.md` so the user can audit.

## Operator commands

| Command                                | What it does                                            |
| -------------------------------------- | ------------------------------------------------------- |
| `bash cluster/workers/setup-macmini.sh`| (Re-)provision the Mac mini worker — idempotent         |
| `bash cluster/workers/setup-rtx.sh`    | (Re-)provision the RTX worker — idempotent              |
| `bash cluster/router/setup-pi.sh`      | (Re-)provision the Pi router — idempotent               |
| `curl pi:8080/healthz` (no auth)       | Inspect worker health snapshot from the router          |
| `journalctl -u kilo-router -f`         | Tail router logs on the Pi                              |
| `make cluster-doctor`                  | Health-check all configured workers (Phase 4)           |
| `KILO_LOCAL=0 kilo-me`                 | Bypass `-lo` routing for this session (Phase 4)         |
| Edit `~/.kilo-me/config/kilo/fallbacks.json` | Adjust per-agent chains; no restart needed         |
| Edit `/etc/kilo-router/config.yaml` + `sudo systemctl reload kilo-router` | Adjust router config |
