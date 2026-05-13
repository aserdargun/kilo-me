# kilo-me local cluster

Run the kilo-me agent fleet on hardware you own. Phase 1 (current) supports a single Ollama worker over Tailscale; later phases add a second worker and a Pi-hosted router.

## Phases

| Phase | What works                                            | Status      |
| ----- | ----------------------------------------------------- | ----------- |
| 1     | Single worker (Mac mini), agents point at it directly | shipped     |
| 2     | Two workers (Mac mini + RTX), dual-provider routing   | shipped     |
| 3     | Pi 5 router with classifier + failover                | shipped     |
| 4     | `cluster-health` MCP + `make cluster-doctor`          | shipped     |
| 5     | Optional 1 B router-assist on the Pi                  | **active**  |

## Phase 1 quickstart

You need:
- One Mac mini M4 (or any Mac with ≥ 16 GB RAM) on the same Tailscale tailnet as your dev machine.
- A Tailscale account (free tier is fine).
- Homebrew installed on the Mac mini.

On the **Mac mini** itself (SSH in or sit at it):

```bash
# 1. Clone this repo (or just scp the script over)
git clone <repo-url> ~/kilo-me
cd ~/kilo-me

# 2. Provision Ollama + Tailscale + LaunchAgent + models
TAILSCALE_HOSTNAME=macmini \
  bash cluster/workers/setup-macmini.sh
```

When the script finishes it prints the worker's Tailscale hostname and `/v1` URL. Copy those — you'll need them on the dev machine.

On the **dev machine** (where you run `kilo-me`):

```bash
# Re-run the global installer; it will prompt for the local cluster URL.
bash install.sh
# When asked for "Local cluster base URL", paste:
#   http://macmini:11434/v1
```

That writes a `local` provider block into `~/.kilo-me/data/kilo/auth.json`:

```json
"local": {
  "type":     "openai-compat",
  "key":      "kilo-local",
  "base_url": "http://macmini:11434/v1"
}
```

Verify it works:

```bash
kilo-me                                # boot Kilo with the kilo-me bundle
# Inside the session:
@coder-lo  scaffold a hello-world Python package
```

If you see tool calls firing and Python files getting written, Phase 1 is live.

## What's installed on the Mac mini

The script is idempotent — re-run it any time. It installs:

| Component   | Source                  | Configuration                                   |
| ----------- | ----------------------- | ----------------------------------------------- |
| Tailscale   | `brew --cask tailscale` | Hostname: `${TAILSCALE_HOSTNAME}` (default `macmini`) |
| Ollama      | `brew --cask ollama`    | Bound to Tailscale IP only; `OLLAMA_KEEP_ALIVE=24h` |
| LaunchAgent | `~/Library/LaunchAgents/ai.kilo-me.ollama.plist` | Auto-starts on login |
| Logs        | `~/Library/Logs/kilo-me-ollama.{out,err}`        |                  |

Model set (soft tier — total ~25–30 GB on disk):

- `qwen3-coder:7b-instruct-q5_K_M` — primary coder
- `llama3.3:8b-instruct-q5_K_M` — memory curator + accountant
- `phi-4:14b-q4_K_M` — debugger soft alt
- `gemma3:4b` — cheap fallback

## Routing in Phase 1

Without the Pi router, every `-lo` agent request goes to the Mac mini. The fallback chains in `~/.kilo-me/config/kilo/fallbacks.json` are already configured to escalate to cloud if the Mac is unreachable, so the worst case is "slower / more expensive cloud call," not "task fails."

`architect-lo`, `coder-lo`, and `debugger-lo` are pinned to **hard-tier model slugs** even though they currently land on the Mac mini. Those slugs (e.g. `qwen3-coder:14b-q8`) won't be present on the soft worker, so the first request to a hard-tier agent will fail and immediately escalate via Rule 06 to the soft-tier `7b` model, then to cloud. That's fine in Phase 1 — Phase 2 brings the slugs to life when the RTX joins.

If you want **all** `-lo` agents to use only the soft-tier slugs in Phase 1, edit `~/.kilo-me/config/kilo/fallbacks.json` and move `local/qwen3-coder:7b-instruct-q5_K_M` to position 0 in `architect-lo`, `coder-lo`, and `debugger-lo`. The MCP `pick_fallback` tool reads the file on every call; no restart needed.

## Phase 2 quickstart — add the RTX

On the **Linux box with the RTX** (Ubuntu/Debian recommended; needs `nvidia-smi` working):

```bash
# Clone (or scp) the repo
git clone <repo-url> ~/kilo-me && cd ~/kilo-me

# Provision Tailscale + Ollama + systemd override + hard-tier model pulls
TAILSCALE_HOSTNAME=rtx \
  bash cluster/workers/setup-rtx.sh
```

The script:
- Verifies the NVIDIA driver (`nvidia-smi`). Does NOT install one — kernel-specific.
- Installs Tailscale via the official apt repo and joins your tailnet.
- Installs Ollama via the official installer (which creates `ollama.service`).
- Writes `/etc/systemd/system/ollama.service.d/kilo-me.conf` to bind Ollama to the Tailscale IP and set `OLLAMA_KEEP_ALIVE=24h`.
- Pulls the hard-tier model set (~50 GB total). Use `MODEL_SET=safe` for lower-VRAM quants if you OOM.
- Runs a tool-calling smoke test against `qwen3-coder:14b`.

On the **dev machine** (re-run the installer — there's a NEW prompt):

```bash
bash install.sh
# At "Hard-tier base URL", paste:
#   http://rtx:11434/v1
# Soft-tier URL stays as you set it in Phase 1 (http://macmini:11434/v1)
```

This writes a second provider block to `auth.json`:

```json
"local":      { "type": "openai-compat", "key": "kilo-local", "base_url": "http://macmini:11434/v1" },
"local-hard": { "type": "openai-compat", "key": "kilo-local", "base_url": "http://rtx:11434/v1" }
```

Now the routing splits automatically by agent slug:

| Agent                  | Provider     | Worker      |
| ---------------------- | ------------ | ----------- |
| `architect-lo`         | `local-hard` | RTX         |
| `coder-lo`             | `local-hard` | RTX         |
| `debugger-lo`          | `local-hard` | RTX         |
| `memory-curator-lo`    | `local`      | Mac mini    |
| `cheap-fallback-lo`    | `local`      | Mac mini    |
| `accountant-lo`        | `local`      | Mac mini    |
| Built-in `architect` / `code` / `debug` | `local-hard` | RTX |

`fallbacks.json` is pre-configured so any hard-tier failure walks `local-hard → local → cloud`, and any soft-tier failure walks `local → local-hard → cloud`. You can edit the chains at `~/.kilo-me/config/kilo/fallbacks.json` to bias the priorities; `pick_fallback` reads the file on every call so no restart is needed.

## Phase 3 quickstart — add the Pi router

Phase 3 is the migration from "static, per-agent routing in `kilo.jsonc`" to "dynamic, per-request routing on a tiny Go service on the Pi". `kilo.jsonc` does not change — the router accepts both `local/<model>` and `local-hard/<model>` requests and decides internally which worker should handle each.

On the **Pi 5** (Raspberry Pi OS, 64-bit):

```bash
git clone <repo-url> ~/kilo-me && cd ~/kilo-me

# Provision: Tailscale + Go + build router binary + systemd unit + config
TAILSCALE_HOSTNAME=pi \
  SOFT_URL=http://macmini:11434 \
  HARD_URL=http://rtx:11434 \
  ROUTER_TOKEN=$(uuidgen) \
  bash cluster/router/setup-pi.sh
```

The script:
- Installs Tailscale (apt) and joins your tailnet.
- Installs `golang-go` (apt) and natively builds `/usr/local/bin/kilo-router` on arm64.
- Renders `/etc/kilo-router/config.yaml` with the URLs and token above (`HARD_URL` blank → soft-only config).
- Installs and enables `/etc/systemd/system/kilo-router.service` (DynamicUser, ReadOnly /etc, MemoryMax=128M, CPUQuota=50%).
- Smoke-tests `/healthz` over the tailnet.

On the **dev machine**:

```bash
bash install.sh
# At "Pi router URL", paste:           http://pi:8080/v1
# At "Router shared token", paste:     <the ROUTER_TOKEN you used above>
```

This rewrites `auth.json` so **both** `local` and `local-hard` provider URLs point at the router:

```json
"local":      { "type": "openai-compat", "key": "<token>", "base_url": "http://pi:8080/v1" },
"local-hard": { "type": "openai-compat", "key": "<token>", "base_url": "http://pi:8080/v1" }
```

`kilo.jsonc` and `fallbacks.json` need no edits. Every `local/...` and `local-hard/...` request now hits the router, which decides per-request which worker to forward to using the rules in `Rule 07 — local cluster routing`.

### What the router observes

The router emits one log line per request:

```
[chat] model=qwen3-coder:14b-instruct-q8_0 tier=hard elapsed=2.4s tokens≈412 tools=3
```

Plus a response header `X-Kilo-Tier: <soft|hard>` and `X-Kilo-Failover: 1` if the chosen tier was dead and traffic was redirected. Tail with `journalctl -u kilo-router -f` on the Pi.

A no-auth `/healthz` endpoint returns the worker snapshot:

```bash
$ curl http://pi:8080/healthz
{
  "workers": {
    "hard": {"healthy": true, "url": "http://rtx:11434", "last_error": "", "last_check": "2026-05-13T14:00:13Z"},
    "soft": {"healthy": true, "url": "http://macmini:11434", "last_error": "", "last_check": "2026-05-13T14:00:13Z"}
  },
  "ts": "2026-05-13T14:00:15Z"
}
```

### Reverting to direct mode (Phase 2)

If you want to take the Pi out of the path temporarily, re-run `install.sh` and leave the **Pi router URL** prompt empty. You'll get the Phase 2 soft+hard URL prompts back, and `auth.json` will point each provider at its own worker again. The router's systemd unit can stay running — it just won't see traffic.

## Phase 5 — opt-in router-assist

When the static rules in `cluster/router/classify.go` hit the ambiguous-multi-tier case (rule 4) — typically a model that's loaded on both workers, a small-to-medium prompt, and a short tools array — they fall back to a deterministic alphabetical pick. That's a sensible default but a coin-flip from the user's perspective: a one-line typo fix and a multi-file refactor both pattern-match to it.

Phase 5 adds a 1B classifier model on the Pi itself that breaks those ties by *reading the actual prompt*. It is **off by default**.

### Enable on a Pi that's already running the router

```bash
# On the Pi:
ENABLE_ASSIST=1 \
  ASSIST_MODEL=qwen2.5:1.5b-instruct-q4_K_M \
  bash cluster/router/setup-pi.sh
```

The script:
- Installs Ollama on the Pi (`curl ... ollama.com/install.sh`).
- Binds Ollama to `127.0.0.1:11434` via a systemd unit override — the assist model is loopback-only and never exposed to the tailnet.
- Pulls `qwen2.5:1.5b-instruct-q4_K_M` (~1 GB) and sets `OLLAMA_KEEP_ALIVE=24h` so it stays resident.
- Rewrites `/etc/kilo-router/config.yaml` to add a `router_assist:` block with `enabled: true`.
- Restarts `kilo-router` — `journalctl -u kilo-router -f` will show `router-assist enabled: …` on startup.

### Disable

Set `router_assist.enabled: false` in `/etc/kilo-router/config.yaml` and reload:

```bash
sudo systemctl reload kilo-router    # or restart, both work
```

The model stays on disk so re-enabling is instant.

### Latency budget

The assist adds 0.5–1.5 s to *only* the rule-4 path (ambiguous multi-tier). All other paths — model-on-one-tier (Rule 1), oversize prompts (Rule 2), many tools (Rule 3) — bypass it entirely. The default `timeout_ms: 1500` is a hard cap; on timeout the request still goes out, just via the static answer. There's no "assist took 30 s and dropped the request" failure mode.

### Auditing

Every assist call is logged with the latency, the raw first-50 chars of the model reply, and which tier won. Tail with:

```bash
journalctl -u kilo-router -f | grep "assist LLM voted"
```

If the assist is consistently voting the same as the static answer it gives nothing for the latency cost — disable it. If it's flipping decisions, look at the `/chat` lines around each `assist LLM voted` to see whether outcomes (failed tool calls, retries) improve.

## Beyond Phase 5

The cluster milestones are done. Future work likely focuses on the agent side — better prompts for `-lo` agents, more sophisticated `route_for` callers, smarter model picks as new Ollama-supported tool-calling models land — rather than the routing infrastructure.

## Troubleshooting

**`tailscale: command not found` after `brew install --cask tailscale`** —
the GUI app needs to be opened once to register the CLI shim. Open `/Applications/Tailscale.app`, log in, then re-run the script.

**Ollama daemon won't bind to the Tailscale IP** — Tailscale IPs only exist after the daemon has logged in. Run `tailscale status` to confirm; re-run the script after.

**`@coder-lo` falls through to cloud immediately** — check `tailscale ping macmini` from the dev machine; if it fails, the tailnet isn't reachable. Also check `curl http://macmini:11434/api/tags` returns a JSON model list.

**Tool calls don't fire** — some Ollama model tags ship with non-tool-calling templates. Run the smoke test at the end of `setup-macmini.sh` against the specific tag you're using; if it doesn't return `tool_calls`, try the q8 variant or a different model on the approved list in Rule 07.
