#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
project_finish.py — close out a project's per-key tracking.

Marks ./.kilo/project.json status="completed", finished_at=<now>, snapshots
the final /keys/{hash}.usage as the project's authoritative total cost, and
optionally disables or deletes the OpenRouter key.

After this runs, ./USAGE.md will show a "Final cost" banner with the
project's lifetime spend on its own key.

Usage
-----
    uv run ~/.config/kilo/scripts/project_finish.py             # mark complete (key kept)
    uv run ~/.config/kilo/scripts/project_finish.py --disable-key
    uv run ~/.config/kilo/scripts/project_finish.py --delete-key
    uv run ~/.config/kilo/scripts/project_finish.py --reopen     # undo (status=active)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
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


def _read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _key_get(admin: str, key_hash: str) -> dict[str, Any] | None:
    headers = {"Authorization": f"Bearer {admin}"}
    with httpx.Client(timeout=15.0, headers=headers) as client:
        r = client.get(f"{_OR_BASE}/keys/{key_hash}")
    if r.status_code >= 400:
        return None
    payload = r.json()
    return payload.get("data") if isinstance(payload, dict) else None


def _key_disable(admin: str, key_hash: str) -> bool:
    headers = {"Authorization": f"Bearer {admin}", "Content-Type": "application/json"}
    with httpx.Client(timeout=15.0, headers=headers) as client:
        r = client.patch(f"{_OR_BASE}/keys/{key_hash}", json={"disabled": True})
    return r.status_code < 400


def _key_delete(admin: str, key_hash: str) -> bool:
    headers = {"Authorization": f"Bearer {admin}"}
    with httpx.Client(timeout=15.0, headers=headers) as client:
        r = client.delete(f"{_OR_BASE}/keys/{key_hash}")
    return r.status_code < 400


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disable-key", action="store_true",
                        help="disable the project's OpenRouter key (reversible)")
    parser.add_argument("--delete-key", action="store_true",
                        help="DELETE the project's OpenRouter key (irreversible)")
    parser.add_argument("--reopen", action="store_true",
                        help="set status back to active (undo a previous finish)")
    args = parser.parse_args()

    if args.disable_key and args.delete_key:
        print("error: --disable-key and --delete-key are mutually exclusive", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] project_finish: %(message)s",
    )
    log = logging.getLogger("project_finish")

    state_path = Path.cwd() / ".kilo" / "project.json"
    state = _read_state(state_path)
    if not state:
        log.error("no .kilo/project.json — run project_init.py first.")
        return 3

    if args.reopen:
        state["status"] = "active"
        state["finished_at"] = None
        _write_state(state_path, state)
        log.info("project reopened: status=active")
        return 0

    admin_key = os.environ.get("OPENROUTER_ADMIN_KEY", "")
    key_hash = state.get("key_hash")
    final_usage: float | None = None
    final_label: str | None = None

    if admin_key and key_hash:
        data = _key_get(admin_key, key_hash)
        if data:
            usage = data.get("usage")
            if isinstance(usage, (int, float)):
                final_usage = float(usage)
            final_label = data.get("label")
        else:
            log.warning("could not fetch /keys/%s for final snapshot", key_hash)

    state["status"] = "completed"
    state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if final_usage is not None:
        state["final_usage"] = final_usage
    if final_label:
        state["key_label"] = final_label

    if args.disable_key:
        if not (admin_key and key_hash):
            log.error("cannot disable: missing admin key or key_hash")
            return 4
        if _key_disable(admin_key, key_hash):
            state["key_disabled"] = True
            log.info("disabled key %s on OpenRouter", key_hash)
        else:
            log.error("PATCH /keys/%s failed", key_hash)
            return 5

    if args.delete_key:
        if not (admin_key and key_hash):
            log.error("cannot delete: missing admin key or key_hash")
            return 4
        if _key_delete(admin_key, key_hash):
            state["key_deleted"] = True
            log.info("DELETED key %s on OpenRouter (irreversible)", key_hash)
        else:
            log.error("DELETE /keys/%s failed", key_hash)
            return 5

    _write_state(state_path, state)
    log.info("project completed: %s", state_path)

    print(json.dumps({
        "ok": True,
        "status": state["status"],
        "finished_at": state["finished_at"],
        "final_usage_usd": final_usage,
        "key_disabled": state.get("key_disabled", False),
        "key_deleted": state.get("key_deleted", False),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
