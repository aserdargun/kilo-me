#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
usage_log.py — append a per-prompt cost snapshot to ./USAGE.log.jsonl.

Designed to be called twice per task by agents (or any tool wrapping a model
call), once before the prompt and once after:

    uv run usage_log.py snapshot --phase start --prompt-id <id>
    # ... agent does the work ...
    uv run usage_log.py snapshot --phase end   --prompt-id <id> --agent <name>

Each call queries OpenRouter `GET /api/v1/keys/{hash}.usage` (account-wide
admin key, key-scoped result) and records `{ts, prompt_id, phase, agent,
key_usage}`. The cost of a single prompt is the delta between paired
start/end rows; usage_report.py joins them.

Why poll /keys/{hash} rather than /generation?
- /keys/{hash}.usage is a single fast call; one HTTP per snapshot.
- /generation requires the response generation_id, which we'd have to scrape
  from each chat-completion response — that means proxying every model call.
- Polling per task is accurate at task grain (the only grain agents see).

Reads:
  ./.kilo/project.json   for the key_hash
  $OPENROUTER_ADMIN_KEY  for the auth header

Writes:
  ./USAGE.log.jsonl      one JSON object per line
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


def _project_state(cwd: Path) -> dict[str, Any]:
    p = cwd / ".kilo" / "project.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _fetch_key_usage(admin_key: str, key_hash: str) -> tuple[float | None, str]:
    headers = {"Authorization": f"Bearer {admin_key}"}
    try:
        with httpx.Client(timeout=10.0, headers=headers) as client:
            r = client.get(f"{_OR_BASE}/keys/{key_hash}")
    except httpx.HTTPError as exc:
        return None, f"request failed: {exc}"
    if r.status_code >= 400:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    try:
        payload = r.json()
    except ValueError:
        return None, "non-JSON body"
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, f"unexpected shape: {payload}"
    usage = data.get("usage")
    if not isinstance(usage, (int, float)):
        return None, f"no numeric .data.usage in response: {data}"
    return float(usage), ""


def _append_log(cwd: Path, row: dict[str, Any]) -> Path:
    p = Path(os.environ.get("USAGE_LOG_PATH") or (cwd / "USAGE.log.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return p


def cmd_snapshot(args: argparse.Namespace) -> int:
    log = logging.getLogger("usage_log")
    cwd = Path.cwd()

    state = _project_state(cwd)
    key_hash = args.key_hash or state.get("key_hash") or os.environ.get("OPENROUTER_KEY_HASH")
    if not key_hash:
        log.error(
            "no key_hash. Either run `project_init.py` first, pass --key-hash, "
            "or set $OPENROUTER_KEY_HASH."
        )
        return 2

    admin_key = os.environ.get("OPENROUTER_ADMIN_KEY", "")
    if not admin_key:
        log.error("OPENROUTER_ADMIN_KEY is not set.")
        return 2

    usage, err = _fetch_key_usage(admin_key, key_hash)
    if usage is None:
        log.error("could not fetch /keys/%s: %s", key_hash, err)
        # Still log the failure so the trail records the attempt.
        _append_log(cwd, {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prompt_id": args.prompt_id,
            "phase": args.phase,
            "agent": args.agent,
            "error": err,
        })
        return 4

    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "prompt_id": args.prompt_id,
        "phase": args.phase,
        "agent": args.agent,
        "key_usage": usage,
    }
    if args.model:
        row["model"] = args.model
    if args.tag:
        row["tag"] = args.tag
    path = _append_log(cwd, row)
    log.info("snapshot %s/%s usage=$%.6f → %s",
             args.prompt_id, args.phase, usage, path)
    print(json.dumps(row))
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    """Quick stdout summary of paired prompts (no markdown — for shell use)."""
    cwd = Path.cwd()
    log_path = Path(os.environ.get("USAGE_LOG_PATH") or (cwd / "USAGE.log.jsonl"))
    if not log_path.is_file():
        print("no USAGE.log.jsonl yet")
        return 0

    starts: dict[str, dict[str, Any]] = {}
    pairs: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "key_usage" not in row or "prompt_id" not in row:
            continue
        pid = row["prompt_id"]
        if row.get("phase") == "start":
            starts[pid] = row
        elif row.get("phase") == "end" and pid in starts:
            s = starts.pop(pid)
            pairs.append({
                "prompt_id": pid,
                "agent": row.get("agent") or s.get("agent"),
                "started": s["ts"],
                "ended": row["ts"],
                "cost": float(row["key_usage"]) - float(s["key_usage"]),
            })

    pairs.sort(key=lambda p: p["cost"], reverse=True)
    total = sum(p["cost"] for p in pairs)
    print(f"completed prompts: {len(pairs)}   total cost: ${total:.4f}")
    for p in pairs[: args.limit]:
        print(f"  ${p['cost']:.4f}  {p['prompt_id']}  ({p['agent'] or '?'})")
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] usage_log: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Per-prompt OpenRouter cost logger.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="record a {start|end} snapshot")
    p_snap.add_argument("--phase", choices=["start", "end"], required=True)
    p_snap.add_argument("--prompt-id", required=True,
                        help="stable id; the same value must be used for start and end")
    p_snap.add_argument("--agent", default=None, help="agent name (e.g. coder-ch)")
    p_snap.add_argument("--model", default=None,
                        help="model slug (e.g. local-hard/qwen3-coder:14b...); "
                             "lets usage_report.py attribute prompts to soft/hard/cloud tiers")
    p_snap.add_argument("--key-hash", default=None,
                        help="override key hash (default: read from .kilo/project.json)")
    p_snap.add_argument("--tag", default=None, help="optional free-form label")
    p_snap.set_defaults(func=cmd_snapshot)

    p_sum = sub.add_parser("summary", help="print top-N prompt costs to stdout")
    p_sum.add_argument("--limit", type=int, default=10)
    p_sum.set_defaults(func=cmd_summary)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
