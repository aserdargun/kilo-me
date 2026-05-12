#!/usr/bin/env bash
# =============================================================================
# uninstall.sh — remove kilo-me from ~/.kilo-me/ and ~/.local/bin/kilo-me
#
# By default this REMOVES code and config but PRESERVES your user data and
# credentials:
#   - $KILO_HOME/memory.sqlite       (prompt history)
#   - $KILO_HOME/chroma/             (Mermaid embeddings)
#   - $KILO_HOME/decisions/          (promoted patterns awaiting sync)
#   - $KILO_DATA_HOME/auth.json      (your API keys)
#   - $KILO_STATE_HOME/model.json    (per-agent model pinning)
#   - $KILO_ME_SHIM                  (the kilo-me wrapper itself — removed only on --purge)
#
# Pass --purge to also delete user data, credentials, the state dir, AND the
# `kilo-me` wrapper on PATH. Pass --keep-auth (with --purge) to remove user
# data but preserve auth.json — useful for re-installs.
#
# On Windows (Git Bash / MSYS), running kilo.exe processes hold SQLite/Kuzu
# files open and rm fails with "Device or resource busy". Pass --kill to
# taskkill any kilo.exe processes before removal.
# =============================================================================

set -euo pipefail

# Match install.sh's defaults — install root is ~/.kilo-me/, NOT the system XDG
# defaults (those belong to vanilla Kilo).
KILO_ME_BASE="${KILO_ME_BASE:-$HOME/.kilo-me}"
KILO_HOME="${KILO_HOME:-$KILO_ME_BASE/config/kilo}"
KILO_DATA_HOME="${KILO_DATA_HOME:-$KILO_ME_BASE/data/kilo}"
KILO_STATE_HOME="${KILO_STATE_HOME:-$KILO_ME_BASE/state/kilo}"
KILO_ME_SHIM="${KILO_ME_SHIM:-$HOME/.local/bin/kilo-me}"

PURGE=0
KEEP_AUTH=0
KILL=0

for arg in "$@"; do
  case "$arg" in
    --purge)     PURGE=1 ;;
    --keep-auth) KEEP_AUTH=1 ;;
    --kill)      KILL=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--purge] [--keep-auth] [--kill]

  --purge       also delete memory.sqlite, chroma/, decisions/, AND auth.json
  --keep-auth   when used with --purge, preserve auth.json
                (no effect without --purge — auth.json is preserved by default)
  --kill        on Windows, taskkill any running kilo.exe processes before
                removing files (frees SQLite/Kuzu locks). No-op on non-Windows.
USAGE
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# -----------------------------------------------------------------------------
# Windows file-lock helper: kilo.exe (and child uv/python processes spawned by
# the MCP servers) hold memory.sqlite, kilo.db, and the Kuzu graph dir open.
# On Git Bash / MSYS, removing them mid-run yields "Device or resource busy".
# --kill uses taskkill to force-terminate them before we touch the filesystem.
# -----------------------------------------------------------------------------
kill_kilo_processes() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) ;;
    *) return 0 ;;  # non-Windows: nothing to do
  esac
  if ! command -v taskkill >/dev/null 2>&1; then
    echo "warning: --kill requested but taskkill not on PATH; skipping" >&2
    return 0
  fi
  local killed=0
  for img in kilo.exe; do
    if tasklist 2>/dev/null | grep -qi "^$img"; then
      echo "killing running $img processes"
      taskkill //F //IM "$img" >/dev/null 2>&1 || true
      killed=1
    fi
  done
  [ "$killed" = "1" ] || echo "no kilo.exe processes running"
}

if [ "$KILL" = "1" ]; then
  kill_kilo_processes
fi

if [ ! -d "$KILO_HOME" ] && [ ! -d "$KILO_DATA_HOME" ] && [ ! -d "$KILO_STATE_HOME" ] && [ ! -e "$KILO_ME_SHIM" ]; then
  echo "nothing to uninstall — none of $KILO_HOME, $KILO_DATA_HOME, $KILO_STATE_HOME, $KILO_ME_SHIM exist"
  exit 0
fi

# -----------------------------------------------------------------------------
# Purge mode: nuke the entire trees so nothing survives (logs, caches, state,
# rendered configs, the Kuzu graph dir, etc.). --keep-auth preserves auth.json
# across the wipe by stashing+restoring it.
# -----------------------------------------------------------------------------
if [ "$PURGE" = "1" ]; then
  STASHED_AUTH=""
  if [ "$KEEP_AUTH" = "1" ] && [ -f "$KILO_DATA_HOME/auth.json" ]; then
    STASHED_AUTH="$(mktemp)"
    cp "$KILO_DATA_HOME/auth.json" "$STASHED_AUTH"
    echo "stashed auth.json (--keep-auth) → will restore after purge"
  fi

  if [ -d "$KILO_HOME" ]; then
    echo "purging $KILO_HOME (entire tree)"
    rm -rf "$KILO_HOME"
  fi
  if [ -d "$KILO_DATA_HOME" ]; then
    echo "purging $KILO_DATA_HOME (entire tree)"
    rm -rf "$KILO_DATA_HOME"
  fi
  if [ -d "$KILO_STATE_HOME" ]; then
    echo "purging $KILO_STATE_HOME (entire tree)"
    rm -rf "$KILO_STATE_HOME"
  fi

  if [ -n "$STASHED_AUTH" ]; then
    mkdir -p "$KILO_DATA_HOME"
    mv "$STASHED_AUTH" "$KILO_DATA_HOME/auth.json"
    chmod 600 "$KILO_DATA_HOME/auth.json"
    echo "restored auth.json → $KILO_DATA_HOME/auth.json"
  fi

  # Tidy the install root if it's empty after the above.
  if [ -d "$KILO_ME_BASE" ] && [ -z "$(ls -A "$KILO_ME_BASE" 2>/dev/null)" ]; then
    rmdir "$KILO_ME_BASE"
    echo "removed empty $KILO_ME_BASE"
  fi

  # Remove the kilo-me wrapper from PATH (bash shim + the Windows .cmd companion).
  if [ -e "$KILO_ME_SHIM" ]; then
    echo "removing $KILO_ME_SHIM"
    rm -f "$KILO_ME_SHIM"
  fi
  if [ -e "${KILO_ME_SHIM}.cmd" ]; then
    echo "removing ${KILO_ME_SHIM}.cmd"
    rm -f "${KILO_ME_SHIM}.cmd"
  fi
else
  # -----------------------------------------------------------------------------
  # Default mode: remove code/config, preserve user data + credentials
  # -----------------------------------------------------------------------------
  if [ -d "$KILO_HOME" ]; then
    echo "removing code and config from $KILO_HOME"
    rm -rf "$KILO_HOME/agents"
    rm -rf "$KILO_HOME/rules"
    rm -rf "$KILO_HOME/mcp_servers"
    rm -rf "$KILO_HOME/scripts"
    rm -rf "$KILO_HOME/bin"
    rm -f  "$KILO_HOME/kilo.jsonc"
    rm -f  "$KILO_HOME/mcp.json"
    rm -f  "$KILO_HOME/fallbacks.json"
    rm -f  "$KILO_HOME/models.curated.json"
    rm -f  "$KILO_HOME"/kilo.jsonc.bak.*
    rm -f  "$KILO_HOME"/refresh.log "$KILO_HOME"/sync.log
  fi

  # Tidy empty dirs (only relevant when not purging — purge already removed them)
  if [ -d "$KILO_HOME" ] && [ -z "$(ls -A "$KILO_HOME" 2>/dev/null)" ]; then
    rmdir "$KILO_HOME"
    echo "removed empty $KILO_HOME"
  elif [ -d "$KILO_HOME" ]; then
    echo "kept $KILO_HOME (still contains data — pass --purge to remove)"
  fi

  if [ -d "$KILO_DATA_HOME" ] && [ -z "$(ls -A "$KILO_DATA_HOME" 2>/dev/null)" ]; then
    rmdir "$KILO_DATA_HOME"
    echo "removed empty $KILO_DATA_HOME"
  elif [ -d "$KILO_DATA_HOME" ]; then
    echo "kept $KILO_DATA_HOME (contains auth.json — pass --purge to remove)"
  fi

  if [ -d "$KILO_STATE_HOME" ]; then
    echo "kept $KILO_STATE_HOME (contains model.json — pass --purge to remove)"
  fi

  if [ -e "$KILO_ME_SHIM" ]; then
    echo "kept $KILO_ME_SHIM (the kilo-me wrapper — pass --purge to remove)"
  fi
fi

echo "done. uv's per-script cache is left intact at ~/.cache/uv/."
