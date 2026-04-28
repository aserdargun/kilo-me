#!/usr/bin/env bash
# =============================================================================
# uninstall.sh — remove kilo-me from ~/.config/kilo/ + ~/.local/share/kilo/
#
# By default this REMOVES code and config but PRESERVES your user data and
# credentials:
#   - $KILO_HOME/memory.sqlite      (prompt history)
#   - $KILO_HOME/chroma/             (Mermaid embeddings)
#   - $KILO_HOME/decisions/          (promoted patterns awaiting sync)
#   - $KILO_DATA_HOME/auth.json      (your API keys)
#
# Pass --purge to also delete user data + credentials. Pass --keep-auth (with
# --purge) to remove user data but preserve auth.json — useful for re-installs.
# =============================================================================

set -euo pipefail

XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
XDG_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
KILO_HOME="${KILO_HOME:-$XDG_CONFIG_HOME/kilo}"
KILO_DATA_HOME="${KILO_DATA_HOME:-$XDG_DATA_HOME/kilo}"

PURGE=0
KEEP_AUTH=0

for arg in "$@"; do
  case "$arg" in
    --purge)     PURGE=1 ;;
    --keep-auth) KEEP_AUTH=1 ;;
    -h|--help)
      cat <<USAGE
Usage: $0 [--purge] [--keep-auth]

  --purge       also delete memory.sqlite, chroma/, decisions/, AND auth.json
  --keep-auth   when used with --purge, preserve auth.json
                (no effect without --purge — auth.json is preserved by default)
USAGE
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

if [ ! -d "$KILO_HOME" ] && [ ! -d "$KILO_DATA_HOME" ]; then
  echo "nothing to uninstall — neither $KILO_HOME nor $KILO_DATA_HOME exists"
  exit 0
fi

# -----------------------------------------------------------------------------
# Always remove: code, rendered configs, generated logs/caches
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
  rm -f  "$KILO_HOME/models.curated.json"
  rm -f  "$KILO_HOME"/kilo.jsonc.bak.*
  rm -f  "$KILO_HOME"/refresh.log "$KILO_HOME"/sync.log
fi

# Note: --purge also removes the Kuzu graph store (graph.kuzu) below.

# -----------------------------------------------------------------------------
# Conditionally remove: user data + credentials
# -----------------------------------------------------------------------------
if [ "$PURGE" = "1" ]; then
  echo "purging user data: memory.sqlite, chroma/, graph.kuzu/, decisions/, adr/"
  rm -f  "$KILO_HOME/memory.sqlite" \
         "$KILO_HOME/memory.sqlite-journal" \
         "$KILO_HOME/memory.sqlite-wal" \
         "$KILO_HOME/memory.sqlite-shm"
  rm -rf "$KILO_HOME/chroma" \
         "$KILO_HOME/graph.kuzu" \
         "$KILO_HOME/decisions" \
         "$KILO_HOME/adr"

  if [ "$KEEP_AUTH" = "1" ]; then
    echo "keeping auth.json (--keep-auth)"
  elif [ -f "$KILO_DATA_HOME/auth.json" ]; then
    echo "purging credentials: $KILO_DATA_HOME/auth.json"
    rm -f "$KILO_DATA_HOME/auth.json"
  fi
fi

# -----------------------------------------------------------------------------
# Tidy up empty directories
# -----------------------------------------------------------------------------
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

echo "done. uv's per-script cache is left intact at ~/.cache/uv/."
