#!/usr/bin/env bash
# =============================================================================
# setup-macmini.sh — provision the Mac mini M4 as the kilo-me SOFT-tier worker.
#
# Idempotent. Safe to re-run. Run this *on the Mac mini itself* (over SSH or
# locally), not from the dev machine.
#
# What it does:
#   1. Installs Tailscale (via Homebrew) and joins the user's tailnet.
#   2. Installs Ollama (via Homebrew) and configures it to listen on the
#      Tailscale interface only — never the public LAN.
#   3. Pulls the soft-tier model set: qwen3-coder:7b, llama3.3:8b, phi-4:14b,
#      gemma3:4b. Each ~5–10 GB; total ~25–30 GB. Skips models already pulled.
#   4. Writes a LaunchAgent so Ollama starts on login with OLLAMA_KEEP_ALIVE=24h
#      and OLLAMA_HOST bound to the Tailscale IP.
#   5. Smoke-tests the OpenAI-compatible endpoint with a tool-call request.
#
# Override knobs (env vars):
#   TAILSCALE_AUTHKEY   non-interactive tailnet join (otherwise prompts)
#   TAILSCALE_HOSTNAME  override the node name on the tailnet (default: macmini)
#   OLLAMA_PORT         override the Ollama listen port (default: 11434)
#   SKIP_MODEL_PULL=1   set up Ollama but don't pre-pull models (faster re-run)
#   SKIP_TAILSCALE=1    you've already joined the tailnet by other means
# =============================================================================

set -euo pipefail

YEL=$'\033[0;33m'
GRN=$'\033[0;32m'
RED=$'\033[0;31m'
NC=$'\033[0m'

log()  { printf "${GRN}[setup-macmini]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[setup-macmini]${NC} %s\n" "$*"; }
die()  { printf "${RED}[setup-macmini]${NC} %s\n" "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Darwin" ]]; then
  die "this script targets macOS; you appear to be on $(uname -s)"
fi

# -----------------------------------------------------------------------------
# 1. Homebrew (assumed present — install instructions if not)
# -----------------------------------------------------------------------------
if ! command -v brew >/dev/null 2>&1; then
  die "Homebrew is required. Install from https://brew.sh and re-run."
fi
log "brew: $(brew --version | head -1)"

# -----------------------------------------------------------------------------
# 2. Tailscale
# -----------------------------------------------------------------------------
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-macmini}"
if [[ "${SKIP_TAILSCALE:-0}" == "1" ]]; then
  log "SKIP_TAILSCALE=1 — assuming tailnet already joined"
else
  if ! command -v tailscale >/dev/null 2>&1; then
    log "installing tailscale via brew (--cask)"
    brew install --cask tailscale
  fi
  # The GUI app needs to be launched once for the CLI shim to exist.
  if [[ ! -e /Applications/Tailscale.app ]]; then
    die "Tailscale.app not found in /Applications — open it once to finish install"
  fi
  if ! tailscale status >/dev/null 2>&1; then
    if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
      log "joining tailnet non-interactively"
      sudo tailscale up --authkey "$TAILSCALE_AUTHKEY" --hostname "$TAILSCALE_HOSTNAME"
    else
      warn "no TAILSCALE_AUTHKEY provided — opening interactive login"
      sudo tailscale up --hostname "$TAILSCALE_HOSTNAME"
    fi
  fi
fi

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[[ -z "$TAILSCALE_IP" ]] && die "could not resolve Tailscale IPv4 address — is the node logged in?"
log "tailscale ip: $TAILSCALE_IP  (hostname: $TAILSCALE_HOSTNAME)"

# -----------------------------------------------------------------------------
# 3. Ollama
# -----------------------------------------------------------------------------
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
if ! command -v ollama >/dev/null 2>&1; then
  log "installing ollama via brew (--cask)"
  brew install --cask ollama
fi
log "ollama: $(ollama --version 2>&1 | head -1)"

# -----------------------------------------------------------------------------
# 4. LaunchAgent — bind to Tailscale IP, 24h keep-alive
# -----------------------------------------------------------------------------
LAUNCH_AGENT_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT="$LAUNCH_AGENT_DIR/ai.kilo-me.ollama.plist"
mkdir -p "$LAUNCH_AGENT_DIR"

log "writing $LAUNCH_AGENT"
cat > "$LAUNCH_AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>           <string>ai.kilo-me.ollama</string>
  <key>RunAtLoad</key>       <true/>
  <key>KeepAlive</key>       <true/>
  <key>StandardOutPath</key> <string>$HOME/Library/Logs/kilo-me-ollama.out</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/kilo-me-ollama.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OLLAMA_HOST</key>       <string>${TAILSCALE_IP}:${OLLAMA_PORT}</string>
    <key>OLLAMA_KEEP_ALIVE</key> <string>24h</string>
    <key>OLLAMA_NUM_PARALLEL</key><string>2</string>
    <!-- Lock to the tailnet by binding only the Tailscale interface. -->
    <key>OLLAMA_ORIGINS</key>    <string>http://${TAILSCALE_HOSTNAME}:*,http://${TAILSCALE_IP}:*</string>
  </dict>
  <key>ProgramArguments</key>
  <array>
    <string>/Applications/Ollama.app/Contents/Resources/ollama</string>
    <string>serve</string>
  </array>
</dict>
</plist>
PLIST

# Reload the agent if it was already loaded.
launchctl unload "$LAUNCH_AGENT" 2>/dev/null || true
launchctl load "$LAUNCH_AGENT"
log "ollama daemon launched — bound to ${TAILSCALE_IP}:${OLLAMA_PORT}"

# Wait for the daemon to come up (max 30 s).
for i in {1..30}; do
  if curl -fsS "http://${TAILSCALE_IP}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    log "ollama responding on tailnet"
    break
  fi
  sleep 1
done

# -----------------------------------------------------------------------------
# 5. Model pulls — soft-tier set
# -----------------------------------------------------------------------------
SOFT_MODELS=(
  "qwen3-coder:7b-instruct-q5_K_M"
  "llama3.3:8b-instruct-q5_K_M"
  "phi-4:14b-q4_K_M"
  "gemma3:4b"
)

if [[ "${SKIP_MODEL_PULL:-0}" == "1" ]]; then
  warn "SKIP_MODEL_PULL=1 — skipping model pulls"
else
  for model in "${SOFT_MODELS[@]}"; do
    if ollama list | awk 'NR>1 {print $1}' | grep -qx "$model"; then
      log "✓ $model already pulled"
    else
      log "pulling $model (this can take 5–15 min on a typical home link)"
      ollama pull "$model"
    fi
  done
fi

# -----------------------------------------------------------------------------
# 6. Smoke test — tool-calling against the OpenAI-compat endpoint
# -----------------------------------------------------------------------------
log "smoke-testing tool-calling via /v1/chat/completions"
SMOKE_RESPONSE=$(curl -fsS "http://${TAILSCALE_IP}:${OLLAMA_PORT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$(cat <<'JSON'
{
  "model": "qwen3-coder:7b-instruct-q5_K_M",
  "messages": [{"role": "user", "content": "Call add with x=2 y=3."}],
  "tools": [{
    "type": "function",
    "function": {
      "name": "add",
      "description": "Add two integers and return the sum.",
      "parameters": {
        "type": "object",
        "properties": {
          "x": {"type": "integer"},
          "y": {"type": "integer"}
        },
        "required": ["x", "y"]
      }
    }
  }],
  "tool_choice": "auto"
}
JSON
)")

if echo "$SMOKE_RESPONSE" | grep -q '"tool_calls"'; then
  log "✓ smoke test passed — model invoked the add() tool"
else
  warn "smoke test did NOT trigger a tool call. Response:"
  echo "$SMOKE_RESPONSE" | head -20
  warn "this usually means the model file lacks a proper tool-calling template;"
  warn "try: ollama pull qwen3-coder:7b-instruct-q8_0  (the q8 variant is sometimes more reliable)"
fi

# -----------------------------------------------------------------------------
# 7. Print the values the dev-machine installer will need
# -----------------------------------------------------------------------------
cat <<EOF

${GRN}===== Mac mini soft-tier worker ready =====${NC}

Tailscale hostname : ${TAILSCALE_HOSTNAME}
Tailscale IPv4     : ${TAILSCALE_IP}
Ollama endpoint    : http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1
LaunchAgent        : ${LAUNCH_AGENT}
Logs               : ~/Library/Logs/kilo-me-ollama.{out,err}

Models pulled:
$(ollama list | awk 'NR>1 {print "  - " $1}')

On your DEV machine, point kilo-me at this worker:

  ${YEL}bash install.sh${NC}
  (when prompted) Local cluster base URL: http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1

Or set it directly in ~/.kilo-me/data/kilo/auth.json:

  "local": {
    "type":     "openai-compat",
    "key":      "<any-non-empty-string-ollama-ignores-it>",
    "base_url": "http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1"
  }

Phase 2 will join the RTX worker; for now all -lo agents land here.
EOF
