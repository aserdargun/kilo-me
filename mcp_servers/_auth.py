"""
_auth.py — kilo-me credential loader (single source of truth).

Reads $XDG_DATA_HOME/kilo/auth.json (default ~/.local/share/kilo/auth.json)
and populates os.environ with its contents. Existing env vars are NEVER
overridden — this lets shell-level secrets win over the file.

Path priority (first hit wins):
    1. $AUTH_FILE                                (explicit override)
    2. $XDG_DATA_HOME/kilo/auth.json
    3. ~/.local/share/kilo/auth.json             (default)

Each MCP server and helper script imports this module via importlib at
startup, before any env-dependent reads. We use file-spec import (not a
package import) to keep each server file self-contained for `uv run --script`.

Supported auth.json schemas
---------------------------
Kilo native format (provider objects):

    {
      "openrouter": {"type": "api", "key": "sk-or-v1-..."}
    }

    Provider keys are mapped to env vars as: <PROVIDER>_API_KEY
    e.g. "openrouter" → OPENROUTER_API_KEY

Flat format (legacy / additional vars):

    {
      "OPENROUTER_API_KEY": "sk-or-v1-...",
      "BP_REPO_PATH":       "/Users/you/code/kilo-best-practices",
      "BP_REMOTE_BRANCH":   "main",
      "BP_AUTO_MERGE":      "0"
    }

Both formats can coexist in the same file. Flat string values are set
directly; provider objects extract the "key" field. All other value types
are silently skipped. Permissions checked: a stderr warning fires if mode
is wider than 0600 (group/world readable).
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def _resolve_auth_path() -> Path:
    """Return the path to auth.json based on env-var precedence."""
    explicit = os.environ.get("AUTH_FILE")
    if explicit:
        return Path(explicit).expanduser()
    xdg_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg_data) / "kilo" / "auth.json"


def _check_perms(path: Path) -> None:
    """Warn (don't fail) on world/group-readable secrets."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"warning: {path} has loose permissions ({oct(mode)[-3:]}); "
            "run `chmod 600` on it",
            file=sys.stderr,
        )


def load_auth(path: Path | None = None) -> dict[str, str]:
    """Load auth.json into os.environ. Returns the merged dict for inspection.

    Existing environment variables are NEVER overwritten. Returns an empty
    dict if the file is missing or unreadable (silent — this is by design,
    so an explicit env var setup keeps working without the file).
    """
    target = path or _resolve_auth_path()
    if not target.is_file():
        return {}
    _check_perms(target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {target}: {exc}", file=sys.stderr)
        return {}

    loaded: dict[str, str] = {}
    if not isinstance(data, dict):
        print(f"warning: {target} root is not a JSON object — ignoring", file=sys.stderr)
        return {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, str):
            # Flat format: KEY = "value"
            loaded[k] = v
            os.environ.setdefault(k, v)
        elif isinstance(v, dict) and isinstance(v.get("key"), str):
            # Kilo native format: "openrouter": {"type": "api", "key": "..."}
            env_key = f"{k.upper()}_API_KEY"
            loaded[env_key] = v["key"]
            os.environ.setdefault(env_key, v["key"])
    return loaded


def auth_path() -> Path:
    """Public helper for diagnostic / setup scripts."""
    return _resolve_auth_path()


# Importing the module triggers the load. Scripts that want to defer or
# re-load should call load_auth() explicitly.
load_auth()


if __name__ == "__main__":
    # Diagnostic: `python _auth.py` prints which keys were loaded.
    p = auth_path()
    if not p.is_file():
        print(f"auth.json not found at {p}")
        sys.exit(1)
    keys = load_auth(p)
    print(f"loaded {len(keys)} key(s) from {p}:")
    for k in sorted(keys):
        # Mask values — show only the first 6 chars
        v = keys[k]
        masked = (v[:6] + "…" + f"({len(v)} chars)") if len(v) > 8 else "***"
        print(f"  {k} = {masked}")
