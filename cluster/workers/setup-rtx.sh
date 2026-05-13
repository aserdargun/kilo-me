#!/usr/bin/env bash
# =============================================================================
# setup-rtx.sh — provision an Ubuntu/Debian Linux box as the kilo-me HARD-tier
# worker (NVIDIA CUDA, e.g. RTX 4070 Ti Super 16 GB).
#
# Idempotent. Safe to re-run. Run this *on the GPU host itself* (over SSH or
# locally), not from the dev machine.
#
# What it does:
#   1. Verifies an NVIDIA driver is loaded (does NOT install one — that's
#      kernel-specific and invasive; install via the distro's recommended path
#      first if `nvidia-smi` is missing).
#   2. Installs Tailscale via the official apt repo and joins the user's tailnet.
#   3. Installs Ollama via the official `curl | sh` installer (which sets up
#      its own systemd unit) and overrides the unit to bind to the Tailscale IP.
#   4. Pulls the hard-tier model set: qwen3-coder:14b q8, devstral:22b q5,
#      deepseek-r1-distill-qwen:14b q5, phi-4:14b q4. Total ~50 GB on disk.
#   5. Smoke-tests tool-calling end-to-end.
#
# Override knobs (env vars):
#   TAILSCALE_AUTHKEY   non-interactive tailnet join (otherwise prompts)
#   TAILSCALE_HOSTNAME  override the node name (default: rtx)
#   OLLAMA_PORT         override Ollama listen port (default: 11434)
#   SKIP_MODEL_PULL=1   set up Ollama but don't pre-pull
#   SKIP_TAILSCALE=1    skip if already joined
#   MODEL_SET=safe      pull q6/q5 variants (lower VRAM pressure) instead of q8
# =============================================================================

set -euo pipefail

YEL=$'\033[0;33m'
GRN=$'\033[0;32m'
RED=$'\033[0;31m'
NC=$'\033[0m'

log()  { printf "${GRN}[setup-rtx]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[setup-rtx]${NC} %s\n" "$*"; }
die()  { printf "${RED}[setup-rtx]${NC} %s\n" "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  die "this script targets Linux; you appear to be on $(uname -s)"
fi
if ! command -v systemctl >/dev/null 2>&1; then
  die "systemd is required (this script writes a systemd unit override)"
fi
if [[ $EUID -eq 0 ]]; then
  die "do NOT run as root — script will call sudo when it needs to"
fi

# -----------------------------------------------------------------------------
# 1. NVIDIA driver sanity check
# -----------------------------------------------------------------------------
if ! command -v nvidia-smi >/dev/null 2>&1; then
  die "nvidia-smi not found. Install the NVIDIA driver first:
       sudo apt install nvidia-driver-550  (or equivalent for your distro)
     then reboot and re-run this script."
fi
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
VRAM_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
log "GPU: ${GPU_NAME} (${VRAM_MB} MiB VRAM)"
if [[ "$VRAM_MB" -lt 12000 ]]; then
  warn "GPU has <12 GiB VRAM — the hard-tier model set may OOM."
  warn "Consider running setup-macmini.sh on a different host instead, or set MODEL_SET=safe."
fi

# -----------------------------------------------------------------------------
# 2. Tailscale (official apt repo)
# -----------------------------------------------------------------------------
TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-rtx}"
if [[ "${SKIP_TAILSCALE:-0}" == "1" ]]; then
  log "SKIP_TAILSCALE=1 — assuming tailnet already joined"
else
  if ! command -v tailscale >/dev/null 2>&1; then
    log "installing tailscale via official apt repo"
    curl -fsSL https://tailscale.com/install.sh | sh
  fi
  if ! sudo tailscale status >/dev/null 2>&1; then
    if [[ -n "${TAILSCALE_AUTHKEY:-}" ]]; then
      log "joining tailnet non-interactively"
      sudo tailscale up --authkey "$TAILSCALE_AUTHKEY" --hostname "$TAILSCALE_HOSTNAME"
    else
      warn "no TAILSCALE_AUTHKEY — running interactive login (URL will print)"
      sudo tailscale up --hostname "$TAILSCALE_HOSTNAME"
    fi
  fi
fi

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[[ -z "$TAILSCALE_IP" ]] && die "could not resolve Tailscale IPv4 — is the node logged in?"
log "tailscale ip: $TAILSCALE_IP  (hostname: $TAILSCALE_HOSTNAME)"

# -----------------------------------------------------------------------------
# 3. Ollama (official installer; ships its own systemd unit)
# -----------------------------------------------------------------------------
OLLAMA_PORT="${OLLAMA_PORT:-11434}"
if ! command -v ollama >/dev/null 2>&1; then
  log "installing ollama (this also creates the systemd unit + CUDA setup)"
  curl -fsSL https://ollama.com/install.sh | sh
fi
log "ollama: $(ollama --version 2>&1 | head -1)"

# -----------------------------------------------------------------------------
# 4. systemd override — bind to Tailscale IP, 24h keep-alive
# -----------------------------------------------------------------------------
OVERRIDE_DIR="/etc/systemd/system/ollama.service.d"
OVERRIDE_FILE="${OVERRIDE_DIR}/kilo-me.conf"

log "writing systemd override: ${OVERRIDE_FILE}"
sudo mkdir -p "$OVERRIDE_DIR"
sudo tee "$OVERRIDE_FILE" >/dev/null <<EOF
# Managed by kilo-me/cluster/workers/setup-rtx.sh
# Binds Ollama to the Tailscale interface only — never the public LAN.
[Service]
Environment="OLLAMA_HOST=${TAILSCALE_IP}:${OLLAMA_PORT}"
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_ORIGINS=http://${TAILSCALE_HOSTNAME}:*,http://${TAILSCALE_IP}:*"
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ollama
# Restart so the override takes effect (systemctl start is a no-op if running).
sudo systemctl restart ollama
log "ollama daemon active — bound to ${TAILSCALE_IP}:${OLLAMA_PORT}"

# Wait for the daemon to come up (max 30 s).
for i in {1..30}; do
  if curl -fsS "http://${TAILSCALE_IP}:${OLLAMA_PORT}/api/tags" >/dev/null 2>&1; then
    log "ollama responding on tailnet"
    break
  fi
  sleep 1
done

# -----------------------------------------------------------------------------
# 5. Model pulls — hard-tier set
# -----------------------------------------------------------------------------
if [[ "${MODEL_SET:-default}" == "safe" ]]; then
  log "MODEL_SET=safe — pulling lower-VRAM quants"
  HARD_MODELS=(
    "qwen3-coder:14b-instruct-q6_K"
    "devstral:22b-q4_K_M"
    "deepseek-r1-distill-qwen:14b-q5_K_M"
    "phi-4:14b-q4_K_M"
  )
else
  HARD_MODELS=(
    "qwen3-coder:14b-instruct-q8_0"
    "devstral:22b-q5_K_M"
    "deepseek-r1-distill-qwen:14b-q5_K_M"
    "phi-4:14b-q4_K_M"
  )
fi

if [[ "${SKIP_MODEL_PULL:-0}" == "1" ]]; then
  warn "SKIP_MODEL_PULL=1 — skipping model pulls"
else
  for model in "${HARD_MODELS[@]}"; do
    if ollama list | awk 'NR>1 {print $1}' | grep -qx "$model"; then
      log "✓ $model already pulled"
    else
      log "pulling $model (5–20 min depending on link speed)"
      ollama pull "$model"
    fi
  done
fi

# -----------------------------------------------------------------------------
# 6. Smoke test — tool-calling against the OpenAI-compat endpoint
# -----------------------------------------------------------------------------
SMOKE_MODEL="${HARD_MODELS[0]}"
log "smoke-testing tool-calling on ${SMOKE_MODEL}"
SMOKE_RESPONSE=$(curl -fsS "http://${TAILSCALE_IP}:${OLLAMA_PORT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"${SMOKE_MODEL}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"Call add with x=2 y=3.\"}],
    \"tools\": [{
      \"type\": \"function\",
      \"function\": {
        \"name\": \"add\",
        \"description\": \"Add two integers and return the sum.\",
        \"parameters\": {
          \"type\": \"object\",
          \"properties\": {\"x\": {\"type\": \"integer\"}, \"y\": {\"type\": \"integer\"}},
          \"required\": [\"x\", \"y\"]
        }
      }
    }],
    \"tool_choice\": \"auto\"
  }")

if echo "$SMOKE_RESPONSE" | grep -q '"tool_calls"'; then
  log "✓ smoke test passed — model invoked the add() tool"
else
  warn "smoke test did NOT trigger a tool call. Response (first 20 lines):"
  echo "$SMOKE_RESPONSE" | head -20
  warn "this usually means the model's chat template doesn't include tool-call syntax."
  warn "try a different quant or re-run with MODEL_SET=safe."
fi

# -----------------------------------------------------------------------------
# 7. Print the values the dev-machine installer will need
# -----------------------------------------------------------------------------
cat <<EOF

${GRN}===== RTX hard-tier worker ready =====${NC}

Tailscale hostname : ${TAILSCALE_HOSTNAME}
Tailscale IPv4     : ${TAILSCALE_IP}
Ollama endpoint    : http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1
systemd unit       : /etc/systemd/system/ollama.service (+ kilo-me.conf override)
Logs               : journalctl -u ollama -f

Models pulled:
$(ollama list | awk 'NR>1 {print "  - " $1}')

On your DEV machine, re-run install.sh — there's a NEW prompt for the hard-tier URL:

  ${YEL}bash install.sh${NC}
  (when prompted) Hard-tier base URL: http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1

That writes a second provider block to auth.json:

  "local-hard": {
    "type":     "openai-compat",
    "key":      "<any-non-empty-string>",
    "base_url": "http://${TAILSCALE_HOSTNAME}:${OLLAMA_PORT}/v1"
  }

After that, ${YEL}architect-lo / coder-lo / debugger-lo${NC} (and the built-in
slots architect / code / debug) automatically land on this RTX worker; the
soft-tier agents stay on the Mac mini. Phase 3 introduces the Pi router which
makes this routing dynamic per-request rather than per-agent.
EOF
