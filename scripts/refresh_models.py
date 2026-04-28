#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
#   "fastmcp>=0.4.0",
# ]
# ///
"""
refresh_models.py — daily cron entry point.

Force-refreshes the OpenRouter model catalog out-of-band so the cache is warm
before any agent invocation. Self-contained: imports the openrouter_models
server module by absolute file path so this script works whether installed
globally (~/.config/kilo/scripts/) or run from the repo.

Usage:
    OPENROUTER_API_KEY=sk-or-... uv run scripts/refresh_models.py

Or place the key in $XDG_DATA_HOME/kilo/auth.json (typically
~/.local/share/kilo/auth.json) and this script will pick it up automatically.

Cron:
    0 6 * * *  uv run ~/.config/kilo/scripts/refresh_models.py >> ~/.config/kilo/refresh.log 2>&1
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path


def _load_auth_json() -> None:
    """Load shared auth.json loader from a sibling location (best-effort)."""
    here = Path(__file__).resolve().parent
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config_home / "kilo"))
    candidates = [
        here.parent / "mcp_servers" / "_auth.py",
        kilo_home / "mcp_servers" / "_auth.py",
        Path.home() / ".kilo" / "mcp_servers" / "_auth.py",  # legacy
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_auth_json()


def _load_or_server() -> object:
    """Load openrouter_models/server.py from one of three known locations:

    1. Sibling repo layout: <root>/mcp_servers/openrouter_models/server.py
       (when this script lives at <root>/scripts/refresh_models.py)
    2. XDG global install:  ~/.config/kilo/mcp_servers/openrouter_models/server.py
       (when this script lives at ~/.config/kilo/scripts/refresh_models.py)
    3. Legacy global install: ~/.kilo/mcp_servers/openrouter_models/server.py
       (kept for backwards compatibility with pre-XDG installs)
    """
    here = Path(__file__).resolve().parent
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config_home / "kilo"))
    candidates = [
        here.parent / "mcp_servers" / "openrouter_models" / "server.py",
        kilo_home / "mcp_servers" / "openrouter_models" / "server.py",
        Path.home() / ".kilo" / "mcp_servers" / "openrouter_models" / "server.py",
    ]
    for path in candidates:
        if path.exists():
            spec = importlib.util.spec_from_file_location("_or_server", path)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_or_server"] = mod
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        "could not locate openrouter_models/server.py in:\n  "
        + "\n  ".join(str(p) for p in candidates)
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] refresh_models: %(message)s",
    )
    log = logging.getLogger("refresh_models")

    if not os.environ.get("OPENROUTER_API_KEY"):
        log.error(
            "OPENROUTER_API_KEY is not set; checked env and auth.json. "
            "Set it via: echo '{\"OPENROUTER_API_KEY\":\"sk-or-...\"}' "
            "> ~/.local/share/kilo/auth.json && chmod 600 ~/.local/share/kilo/auth.json"
        )
        return 2

    try:
        server = _load_or_server()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 3

    started = time.time()
    res = server.refresh_catalog(top_n=30)  # type: ignore[attr-defined]
    elapsed = time.time() - started

    if not res.get("refreshed"):
        log.error("refresh failed: %s", res)
        return 1

    log.info(
        "refreshed %d/%d models in %.1fs — top: %s",
        res["count"],
        res.get("total_available", res["count"]),
        elapsed,
        ", ".join(t["id"] for t in res["top"][:5]),
    )

    summary = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "models_count": res["count"],
        "total_available": res.get("total_available"),
        "elapsed_seconds": round(elapsed, 2),
        "cache_path": str(server._CACHE_PATH),  # type: ignore[attr-defined]
        "top_5": [t["id"] for t in res["top"][:5]],
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
