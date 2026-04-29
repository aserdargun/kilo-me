#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
project_init.py — provision a per-project OpenRouter inference key.

Reads $OPENROUTER_ADMIN_KEY (provisioning/management key) from the global
auth.json, calls POST /api/v1/keys to create a new sub-key labeled after the
current directory, then writes:

  ./auth.json          - the new inference key (chmod 600, gitignored)
  ./.kilo/project.json - project state: name, key_hash, created_at, status

Agents in the project use ./auth.json automatically (the per-project resolver
in usage_report.py and the same convention any local Kilo install follows).
The admin key never leaves ~/.local/share/kilo/auth.json — projects only ever
see their own scoped sub-key.

Idempotency
-----------
If ./.kilo/project.json already exists with status=active, this script
refuses unless --force is passed (which would orphan the prior key).

Env knobs
---------
  PROJECT_NAME            override the key/label/dir name
                          (default: current directory basename)
  PROJECT_CREDIT_LIMIT    USD cap on the new key (default: unlimited)
  OPENROUTER_BASE_URL     override API base

Usage
-----
    cd <project>
    uv run ~/.config/kilo/scripts/project_init.py
    PROJECT_CREDIT_LIMIT=20 uv run ~/.config/kilo/scripts/project_init.py
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def _load_global_auth() -> None:
    here = Path(__file__).resolve().parent
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config_home / "kilo"))
    candidates = [
        here.parent / "mcp_servers" / "_auth.py",
        kilo_home / "mcp_servers" / "_auth.py",
        Path.home() / ".kilo" / "mcp_servers" / "_auth.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_global_auth()


_OR_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")


def _provision_key(admin_key: str, name: str, limit: float | None) -> dict[str, Any]:
    body: dict[str, Any] = {"name": name, "label": name}
    if limit is not None:
        body["limit"] = limit
    headers = {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=20.0, headers=headers) as client:
        r = client.post(f"{_OR_BASE}/keys", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"POST /keys failed ({r.status_code}): {r.text[:300]}")
    payload = r.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected /keys response shape: {payload}")
    # Defensive: different OpenRouter docs use 'hash' or 'id' for the handle.
    key_hash = data.get("hash") or data.get("id") or data.get("key_hash")
    api_key = data.get("key")
    if not api_key or not key_hash:
        raise RuntimeError(f"/keys response missing 'key' or 'hash'/'id': {data}")
    return {
        "api_key": api_key,
        "key_hash": key_hash,
        "label": data.get("label") or name,
        "limit": data.get("limit"),
        "raw": data,
    }


def _write_project_auth(path: Path, api_key: str, key_hash: str) -> None:
    payload = {
        "openrouter": {"type": "api", "key": api_key},
        "OPENROUTER_KEY_HASH": key_hash,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600


def _write_project_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a per-project OpenRouter key.")
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing .kilo/project.json (orphans prior key)")
    parser.add_argument("--name", default=None,
                        help="project name (default: $PROJECT_NAME or cwd basename)")
    parser.add_argument("--limit", type=float, default=None,
                        help="USD credit limit on the new key (default: $PROJECT_CREDIT_LIMIT or none)")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] project_init: %(message)s",
    )
    log = logging.getLogger("project_init")

    admin_key = os.environ.get("OPENROUTER_ADMIN_KEY", "")
    if not admin_key:
        log.error(
            "OPENROUTER_ADMIN_KEY is not set. Add it to "
            "~/.local/share/kilo/auth.json as a flat string and re-run."
        )
        return 2

    cwd = Path.cwd()
    name = args.name or os.environ.get("PROJECT_NAME") or cwd.name
    limit = args.limit
    if limit is None:
        env_limit = os.environ.get("PROJECT_CREDIT_LIMIT")
        if env_limit:
            try:
                limit = float(env_limit)
            except ValueError:
                log.warning("PROJECT_CREDIT_LIMIT=%r is not a number, ignoring", env_limit)
                limit = None

    auth_path = cwd / "auth.json"
    state_path = cwd / ".kilo" / "project.json"

    if state_path.is_file() and not args.force:
        try:
            existing = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        log.error(
            "Project already initialized: %s (status=%s, key_hash=%s). "
            "Use --force to overwrite (this orphans the prior key on OpenRouter; "
            "consider running project_finish.py first).",
            state_path, existing.get("status"), existing.get("key_hash"),
        )
        return 3

    log.info("provisioning OpenRouter key: name=%s limit=%s",
             name, f"${limit:.2f}" if limit is not None else "unlimited")
    try:
        result = _provision_key(admin_key, name=name, limit=limit)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.error("key provisioning failed: %s", exc)
        return 4

    _write_project_auth(auth_path, result["api_key"], result["key_hash"])
    log.info("wrote %s (chmod 600)", auth_path)

    state = {
        "name": name,
        "key_hash": result["key_hash"],
        "key_label": result["label"],
        "key_limit": result["limit"],
        "status": "active",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "finished_at": None,
    }
    _write_project_state(state_path, state)
    log.info("wrote %s", state_path)

    print(json.dumps({
        "ok": True,
        "name": name,
        "key_hash": result["key_hash"],
        "key_label": result["label"],
        "key_limit": result["limit"],
        "auth_path": str(auth_path),
        "state_path": str(state_path),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
