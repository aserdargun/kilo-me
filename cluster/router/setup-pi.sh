#!/usr/bin/env bash
# =============================================================================
# setup-pi.sh — provision a Raspberry Pi 5 as the kilo-me cluster router.
#
# Run this on the Pi itself (SSH or sit at it). Idempotent.
#
# What it does:
#   1. Installs Tailscale (official apt repo) and joins your tailnet.
#   2. Installs Go (apt) and builds the kilo-router binary natively on arm64.
#   3. Drops /etc/kilo-router/config.yaml from the template, substituting the
#      Mac mini + RTX URLs you pass via env vars.
#   4. Writes /etc/systemd/system/kilo-router.service and starts it.
#   5. Smoke-tests /healthz over the tailnet.
#
# Override knobs (env vars):
#   TAILSCALE_AUTHKEY    non-interactive tailnet join
#   TAILSCALE_HOSTNAME   default "pi"
#   SOFT_URL             default "http://macmini:11434"   (Mac mini Ollama)
#   HARD_URL             default "http://rtx:11434"       (RTX Ollama; blank → omit)
#   ROUTER_TOKEN         default "kilo-local"             (shared bearer token)
#   ROUTER_PORT          default 8080
#   ENABLE_ASSIST=1      Phase 5: also install Ollama on the Pi + pull a 1B
#                        classifier model and enable router_assist in config
#   ASSIST_MODEL         default "qwen2.5:1.5b-instruct-q4_K_M"
#   SKIP_TAILSCALE=1     skip if already joined
#   SKIP_BUILD=1         reuse an existing /usr/local/bin/kilo-router
# =============================================================================

set -euo pipefail

YEL=$'\033[0;33m'
GRN=$'\033[0;32m'
RED=$'\033[0;31m'
NC=$'\033[0m'

log()  { printf "${GRN}[setup-pi]${NC} %s\n" "$*"; }
warn() { printf "${YEL}[setup-pi]${NC} %s\n" "$*"; }
die()  { printf "${RED}[setup-pi]${NC} %s\n" "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  die "this script targets Linux (Raspberry Pi OS); you appear to be on $(uname -s)"
fi
if [[ "$(uname -m)" != "aarch64" ]] && [[ "$(uname -m)" != "arm64" ]]; then
  warn "expected aarch64, got $(uname -m) — proceeding anyway"
fi
if [[ $EUID -eq 0 ]]; then
  die "do NOT run as root — script calls sudo when it needs to"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME:-pi}"
SOFT_URL="${SOFT_URL:-http://macmini:11434}"
HARD_URL="${HARD_URL:-http://rtx:11434}"
ROUTER_TOKEN="${ROUTER_TOKEN:-kilo-local}"
ROUTER_PORT="${ROUTER_PORT:-8080}"
ENABLE_ASSIST="${ENABLE_ASSIST:-0}"
ASSIST_MODEL="${ASSIST_MODEL:-qwen2.5:1.5b-instruct-q4_K_M}"

# -----------------------------------------------------------------------------
# 1. Tailscale
# -----------------------------------------------------------------------------
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
      warn "no TAILSCALE_AUTHKEY — interactive login URL will print"
      sudo tailscale up --hostname "$TAILSCALE_HOSTNAME"
    fi
  fi
fi

TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[[ -z "$TAILSCALE_IP" ]] && die "could not resolve Tailscale IPv4 — is the node logged in?"
log "tailscale ip: $TAILSCALE_IP  (hostname: $TAILSCALE_HOSTNAME)"

# -----------------------------------------------------------------------------
# 2. Go (apt) + build the router
# -----------------------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" == "1" && -x /usr/local/bin/kilo-router ]]; then
  log "SKIP_BUILD=1 — reusing existing /usr/local/bin/kilo-router"
else
  if ! command -v go >/dev/null 2>&1; then
    log "installing golang via apt"
    sudo apt-get update -qq
    sudo apt-get install -y -qq golang-go
  fi
  log "go: $(go version)"
  log "building kilo-router from $SCRIPT_DIR"
  (
    cd "$SCRIPT_DIR"
    # On the Pi we're already arm64, so the default build is correct.
    go mod tidy
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /tmp/kilo-router .
  )
  sudo install -m 0755 /tmp/kilo-router /usr/local/bin/kilo-router
  rm /tmp/kilo-router
  log "installed /usr/local/bin/kilo-router"
fi

# -----------------------------------------------------------------------------
# 3. Config — render template into /etc/kilo-router/config.yaml
# -----------------------------------------------------------------------------
sudo mkdir -p /etc/kilo-router
CONFIG_FILE=/etc/kilo-router/config.yaml

# Phase 5 — optional 1B router-assist on the Pi itself.
ASSIST_BLOCK=""
if [[ "$ENABLE_ASSIST" == "1" ]]; then
  log "ENABLE_ASSIST=1 — installing local Ollama for router-assist"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  # Bind Ollama to localhost only — the router talks to it loopback.
  # Override the upstream systemd unit so it survives apt upgrades.
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  sudo tee /etc/systemd/system/ollama.service.d/kilo-pi-assist.conf >/dev/null <<'OVR'
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_KEEP_ALIVE=24h"
Environment="OLLAMA_NUM_PARALLEL=1"
OVR
  sudo systemctl daemon-reload
  sudo systemctl enable --now ollama
  sudo systemctl restart ollama
  # Wait a few seconds for ollama to come up before pulling.
  for i in {1..20}; do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 1
  done
  if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "$ASSIST_MODEL"; then
    log "pulling assist model $ASSIST_MODEL (~1 GB; one-time)"
    ollama pull "$ASSIST_MODEL"
  else
    log "✓ $ASSIST_MODEL already pulled"
  fi
  ASSIST_BLOCK=$(cat <<EOF

router_assist:
  enabled: true
  url: "http://127.0.0.1:11434"
  model: "${ASSIST_MODEL}"
  timeout_ms: 1500
  max_chars: 2000
EOF
)
fi

if [[ -f "$CONFIG_FILE" ]]; then
  log "kept existing $CONFIG_FILE (re-run with sudo rm $CONFIG_FILE to regenerate)"
else
  log "writing $CONFIG_FILE"
  HARD_BLOCK=""
  if [[ -n "$HARD_URL" ]]; then
    HARD_BLOCK=$(cat <<EOF

  hard:
    url: "${HARD_URL}"
    models:
      - "qwen3-coder:14b-instruct-q8_0"
      - "devstral:22b-q5_K_M"
      - "deepseek-r1-distill-qwen:14b-q5_K_M"
      - "phi-4:14b-q4_K_M"
EOF
)
  fi
  sudo tee "$CONFIG_FILE" >/dev/null <<EOF
# Generated by cluster/router/setup-pi.sh. Re-running the script will NOT
# overwrite this file unless you delete it first.

server:
  listen: ":${ROUTER_PORT}"

auth:
  token: "${ROUTER_TOKEN}"

workers:
  soft:
    url: "${SOFT_URL}"
    models:
      - "qwen3-coder:7b-instruct-q5_K_M"
      - "llama3.3:8b-instruct-q5_K_M"
      - "phi-4:14b-q4_K_M"
      - "gemma3:4b"${HARD_BLOCK}

routing:
  token_threshold_for_hard: 8000
  tool_count_threshold_for_hard: 5
  default_tier: "soft"${ASSIST_BLOCK}
EOF
fi

# -----------------------------------------------------------------------------
# 4. systemd unit
# -----------------------------------------------------------------------------
UNIT_PATH=/etc/systemd/system/kilo-router.service
log "installing $UNIT_PATH"
sudo install -m 0644 "$SCRIPT_DIR/kilo-router.service" "$UNIT_PATH"

sudo systemctl daemon-reload
sudo systemctl enable --now kilo-router
sudo systemctl restart kilo-router
log "kilo-router.service active"

# -----------------------------------------------------------------------------
# 5. Smoke test
# -----------------------------------------------------------------------------
sleep 2
log "smoke-testing /healthz on the tailnet"
if curl -fsS "http://${TAILSCALE_IP}:${ROUTER_PORT}/healthz" | head -c 500; then
  echo
  log "✓ router responding"
else
  warn "router not responding — check: journalctl -u kilo-router -n 50"
fi

cat <<EOF

${GRN}===== Pi router ready =====${NC}

Tailscale hostname : ${TAILSCALE_HOSTNAME}
Tailscale IPv4     : ${TAILSCALE_IP}
Router endpoint    : http://${TAILSCALE_HOSTNAME}:${ROUTER_PORT}/v1
Health (no-auth)   : http://${TAILSCALE_HOSTNAME}:${ROUTER_PORT}/healthz
Config             : ${CONFIG_FILE}
Logs               : journalctl -u kilo-router -f

Workers proxied:
  soft → ${SOFT_URL}
$( [[ -n "$HARD_URL" ]] && echo "  hard → ${HARD_URL}" )
$( [[ "$ENABLE_ASSIST" == "1" ]] && echo "  router-assist (Phase 5) → 127.0.0.1:11434 model=${ASSIST_MODEL}" )

${YEL}Next step (on your dev machine):${NC}
  bash install.sh
  (when prompted) Pi router URL: http://${TAILSCALE_HOSTNAME}:${ROUTER_PORT}/v1
  (when prompted) Shared token  : ${ROUTER_TOKEN}

That will rewrite auth.json so both the "local" and "local-hard" provider
blocks point at the router — agents continue to use their existing model
slugs (local/… and local-hard/…) and the router decides which worker to
hit per request.
EOF
