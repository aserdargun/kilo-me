#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
usage_report.py — generate a per-project USAGE.md from the OpenRouter API.

Run from inside a project directory; writes ./USAGE.md scoped to that
project's OpenRouter key.

Two-key model
-------------
OpenRouter has no native "project" concept and `/activity` cannot be
filtered by HTTP-Referer / X-Title. The reliable way to scope cost to one
project is to give the project its own inference key:

  PROJECT KEY   per-project inference key. /key returns its label, limit,
                and lifetime usage — these are the headline numbers in
                USAGE.md. Resolution order:
                  1. $OPENROUTER_PROJECT_KEY
                  2. ./.kilo/auth.json   (project-local Kilo config)
                  3. ./auth.json         (project-local file)
                  4. global $OPENROUTER_API_KEY (fallback; not project-scoped)

  ADMIN KEY     optional management/provisioning key. Powers the daily +
                model + provider breakdown via /activity. /activity is
                ACCOUNT-wide, so this section is clearly labeled as such.
                Resolution: $OPENROUTER_ADMIN_KEY (env or any auth.json).

Per-project setup
-----------------
1. Create a key on https://openrouter.ai/keys, label it after the project,
   optionally set a credit limit.
2. Drop it in ./auth.json at the project root (chmod 600):
       { "openrouter": { "type": "api", "key": "sk-or-v1-…" } }
   or as a flat key:
       { "OPENROUTER_PROJECT_KEY": "sk-or-v1-…" }
3. Run `uv run scripts/usage_report.py` from the project root (or
   `make usage-report` if the project has a Makefile).

Endpoints used
--------------
  GET /api/v1/key         — per-key label, limit, lifetime usage
  GET /api/v1/credits     — account credit balance + lifetime usage
  GET /api/v1/activity    — daily/model/provider rollups (account-wide,
                            requires admin key, otherwise skipped)

Cost trend
----------
Each run appends a snapshot (timestamp, key label, lifetime spend, account
context) to ./USAGE.history.jsonl. The report's "Cost trend" section shows
current vs previous spend, the delta since the last run, and a table of the
N most recent snapshots — so the project's USAGE.md is a running ledger,
not just a point-in-time view. The history file is plain JSONL, safe to
commit, and easy to .gitignore.

Env knobs
---------
  USAGE_REPORT_PATH       override output path (default: ./USAGE.md)
  USAGE_HISTORY_PATH      override snapshot log (default: ./USAGE.history.jsonl)
  USAGE_HISTORY_LIMIT     rows shown in trend table (default: 10)
  OPENROUTER_BASE_URL     override API base
  OPENROUTER_APP_TITLE    title used in the report header (default: cwd name)
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx


def _load_global_auth() -> None:
    """Best-effort load of ~/.local/share/kilo/auth.json via the shared loader."""
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


_load_global_auth()


_OR_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_DEFAULT_TITLE = Path.cwd().name


def _read_local_auth(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("could not read %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _key_from_local(data: dict[str, Any]) -> str:
    """Pull a project key out of an auth.json dict (flat or provider form)."""
    v = data.get("OPENROUTER_PROJECT_KEY")
    if isinstance(v, str) and v:
        return v
    prov = data.get("openrouter")
    if isinstance(prov, dict) and isinstance(prov.get("key"), str):
        return prov["key"]
    return ""


def _resolve_project_key() -> tuple[str, str]:
    """Return (key, source_description). Empty key if none found."""
    k = os.environ.get("OPENROUTER_PROJECT_KEY", "")
    if k:
        return k, "$OPENROUTER_PROJECT_KEY"
    cwd = Path.cwd()
    for candidate in [cwd / ".kilo" / "auth.json", cwd / "auth.json"]:
        data = _read_local_auth(candidate)
        if not data:
            continue
        k = _key_from_local(data)
        if k:
            return k, str(candidate)
    k = os.environ.get("OPENROUTER_API_KEY", "")
    if k:
        return k, "$OPENROUTER_API_KEY (global fallback — not project-scoped)"
    return "", ""


def _read_project_state() -> dict[str, Any]:
    """Load .kilo/project.json (written by project_init.py)."""
    p = Path.cwd() / ".kilo" / "project.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_prompt_log() -> list[dict[str, Any]]:
    """Pair start/end snapshots from ./USAGE.log.jsonl into per-prompt rows."""
    p = Path(os.environ.get("USAGE_LOG_PATH") or (Path.cwd() / "USAGE.log.jsonl"))
    if not p.is_file():
        return []
    starts: dict[str, dict[str, Any]] = {}
    pairs: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid = row.get("prompt_id")
        if not pid or "key_usage" not in row:
            continue
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
    return pairs


def _fetch_key_by_hash(admin_key: str, key_hash: str) -> dict[str, Any] | None:
    """Authoritative per-key spend via admin /keys/{hash}."""
    headers = {"Authorization": f"Bearer {admin_key}"}
    try:
        with httpx.Client(timeout=15.0, headers=headers) as client:
            r = client.get(f"{_OR_BASE}/keys/{key_hash}")
    except httpx.HTTPError as exc:
        logging.warning("/keys/%s — %s", key_hash, exc)
        return None
    if r.status_code >= 400:
        logging.warning("/keys/%s — HTTP %d: %s", key_hash, r.status_code, r.text[:200])
        return None
    try:
        payload = r.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _get(client: httpx.Client, path: str) -> tuple[dict[str, Any] | None, str]:
    try:
        r = client.get(f"{_OR_BASE}{path}")
    except httpx.HTTPError as exc:
        msg = f"request failed: {exc}"
        logging.warning("%s — %s", path, msg)
        return None, msg
    if r.status_code >= 400:
        msg = f"HTTP {r.status_code}: {r.text[:200]}"
        logging.warning("%s — %s", path, msg)
        return None, msg
    try:
        return r.json(), ""
    except ValueError:
        return None, "non-JSON body"


def _client(api_key: str, title: str) -> httpx.Client:
    return httpx.Client(
        timeout=20.0,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": os.environ.get("OPENROUTER_REFERER", f"https://local/{title}"),
            "X-Title": title,
        },
    )


def _fmt_usd(v: float | int | None) -> str:
    if v is None:
        return "—"
    return f"${float(v):,.4f}"


def _fmt_int(v: int | float | None) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _history_path() -> Path:
    return Path(os.environ.get("USAGE_HISTORY_PATH") or (Path.cwd() / "USAGE.history.jsonl"))


def _load_history() -> list[dict[str, Any]]:
    path = _history_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _append_history(snapshot: dict[str, Any]) -> Path:
    path = _history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, separators=(",", ":")) + "\n")
    return path


def _activity_rows(activity: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not activity:
        return []
    data = activity.get("data") if isinstance(activity, dict) else None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def _bucket() -> dict[str, float]:
        return {"cost": 0.0, "requests": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "reasoning_tokens": 0}

    by_day: dict[str, dict[str, float]] = defaultdict(_bucket)
    by_model: dict[str, dict[str, float]] = defaultdict(_bucket)
    by_provider: dict[str, dict[str, float]] = defaultdict(_bucket)
    totals = _bucket()

    for row in rows:
        cost = float(row.get("usage") or 0.0)
        reqs = int(row.get("requests") or 0)
        ptok = int(row.get("prompt_tokens") or 0)
        ctok = int(row.get("completion_tokens") or 0)
        rtok = int(row.get("reasoning_tokens") or 0)
        day = str(row.get("date") or "unknown").split(" ", 1)[0]
        model = str(row.get("model") or row.get("model_permaslug") or "unknown")
        provider = str(row.get("provider_name") or "unknown")

        for bucket in (by_day[day], by_model[model], by_provider[provider], totals):
            bucket["cost"] += cost
            bucket["requests"] += reqs
            bucket["prompt_tokens"] += ptok
            bucket["completion_tokens"] += ctok
            bucket["reasoning_tokens"] += rtok

    return {
        "by_day": dict(sorted(by_day.items(), reverse=True)),
        "by_model": dict(sorted(by_model.items(), key=lambda kv: kv[1]["cost"], reverse=True)),
        "by_provider": dict(sorted(by_provider.items(), key=lambda kv: kv[1]["cost"], reverse=True)),
        "totals": totals,
    }


def _render(
    *,
    title: str,
    project_state: dict[str, Any],
    project_key_source: str,
    project_key_data: dict[str, Any] | None,
    project_key_error: str,
    is_global_fallback: bool,
    credits: dict[str, Any] | None,
    admin_available: bool,
    admin_error: str,
    agg: dict[str, Any],
    history: list[dict[str, Any]],
    history_limit: int,
    prompt_pairs: list[dict[str, Any]],
    generated_at: str,
) -> str:
    keyd = (project_key_data or {}).get("data") or {}
    cred = (credits or {}).get("data") or {}

    lines: list[str] = []
    lines.append(f"# Usage — {title}")
    lines.append("")
    lines.append(f"_Generated {generated_at} from the OpenRouter API._")
    lines.append("")

    # ── Final-cost banner (only when project is marked completed) ─────────
    if project_state.get("status") == "completed":
        final = project_state.get("final_usage")
        finished_at = project_state.get("finished_at")
        lines.append("> ✅ **Project completed.**")
        lines.append(">")
        if isinstance(final, (int, float)):
            lines.append(f"> **Final cost:** {_fmt_usd(final)}")
        if finished_at:
            lines.append(f"> Finished at {finished_at}")
        if project_state.get("key_deleted"):
            lines.append("> OpenRouter key was deleted at finish.")
        elif project_state.get("key_disabled"):
            lines.append("> OpenRouter key was disabled at finish.")
        lines.append("")

    # ── Project metadata (when project_init.py was run) ────────────────────
    if project_state:
        lines.append("## Project")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Name | {project_state.get('name') or '—'} |")
        lines.append(f"| Status | {project_state.get('status') or '—'} |")
        lines.append(f"| Created | {project_state.get('created_at') or '—'} |")
        if project_state.get("finished_at"):
            lines.append(f"| Finished | {project_state['finished_at']} |")
        lines.append(f"| Key label | {project_state.get('key_label') or '—'} |")
        lines.append(f"| Key hash | `{project_state.get('key_hash') or '—'}` |")
        if project_state.get("key_limit") is not None:
            lines.append(f"| Key limit | {_fmt_usd(project_state.get('key_limit'))} |")
        lines.append("")

    # ── Project key (the headline) ─────────────────────────────────────────
    lines.append("## Project key spend")
    lines.append("")
    lines.append(f"_Source: `{project_key_source or 'none found'}`._")
    lines.append("")

    if is_global_fallback:
        lines.append(
            "> ⚠️  Falling back to the global `$OPENROUTER_API_KEY`. The numbers "
            "below cover **all** of that key's usage, not just this project. "
            "To get true per-project scope, create a project-specific key on "
            "openrouter.ai/keys and drop it in `./auth.json` (see this "
            "script's docstring)."
        )
        lines.append("")

    if not keyd and project_key_error:
        lines.append(f"_`/key` not available: {project_key_error}._")
        lines.append("")
    elif keyd:
        usage = keyd.get("usage")
        limit = keyd.get("limit")
        remaining = None
        if isinstance(usage, (int, float)) and isinstance(limit, (int, float)):
            remaining = float(limit) - float(usage)
        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Key label | {keyd.get('label') or '—'} |")
        lines.append(f"| Lifetime spend on this key | {_fmt_usd(usage)} |")
        lines.append(f"| Credit limit on this key | {_fmt_usd(limit) if limit is not None else 'unlimited'} |")
        if remaining is not None:
            lines.append(f"| Remaining on this key | {_fmt_usd(remaining)} |")
        lines.append(f"| Free tier | {'yes' if keyd.get('is_free_tier') else 'no'} |")
        lines.append("")

    # ── Cost trend (per-project history) ──────────────────────────────────
    lines.append("## Cost trend")
    lines.append("")
    if not history:
        lines.append("_No history yet — this is the first run for this project._")
        lines.append("")
    else:
        latest = history[-1]
        previous = history[-2] if len(history) >= 2 else None
        latest_usage = float(latest.get("key_usage") or 0.0)
        prev_usage = float(previous.get("key_usage") or 0.0) if previous else None
        delta = (latest_usage - prev_usage) if prev_usage is not None else None

        lines.append("| Metric | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| This run ({latest.get('ts')}) | {_fmt_usd(latest_usage)} |")
        if previous:
            lines.append(f"| Previous run ({previous.get('ts')}) | {_fmt_usd(prev_usage)} |")
            lines.append(f"| Δ since previous run | {_fmt_usd(delta)} |")
        else:
            lines.append("| Previous run | — (first run) |")
            lines.append("| Δ since previous run | — |")
        lines.append("")

        recent = history[-history_limit:]
        if len(recent) > 1:
            lines.append(f"### Recent runs (last {len(recent)})")
            lines.append("")
            lines.append("| Timestamp | Lifetime spend | Δ vs prior | Account usage |")
            lines.append("| --- | ---: | ---: | ---: |")
            prior: float | None = None
            for snap in recent:
                u = float(snap.get("key_usage") or 0.0)
                acct = snap.get("account_usage")
                d = (u - prior) if prior is not None else None
                lines.append(
                    f"| {snap.get('ts')} | {_fmt_usd(u)} | "
                    f"{_fmt_usd(d) if d is not None else '—'} | "
                    f"{_fmt_usd(acct) if acct is not None else '—'} |"
                )
                prior = u
            lines.append("")

    # ── Account context ────────────────────────────────────────────────────
    lines.append("## Account context")
    lines.append("")
    total_credits = cred.get("total_credits")
    lifetime_usage = cred.get("total_usage")
    remaining = None
    if isinstance(total_credits, (int, float)) and isinstance(lifetime_usage, (int, float)):
        remaining = float(total_credits) - float(lifetime_usage)
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Account credits purchased | {_fmt_usd(total_credits)} |")
    lines.append(f"| Account lifetime usage | {_fmt_usd(lifetime_usage)} |")
    lines.append(f"| Account remaining balance | {_fmt_usd(remaining)} |")
    lines.append("")

    # ── Per-prompt costs (from USAGE.log.jsonl, written by usage_log.py) ──
    if prompt_pairs:
        total = sum(p["cost"] for p in prompt_pairs)
        by_agent: dict[str, dict[str, float]] = {}
        for p in prompt_pairs:
            a = p.get("agent") or "unknown"
            slot = by_agent.setdefault(a, {"cost": 0.0, "count": 0})
            slot["cost"] += p["cost"]
            slot["count"] += 1

        lines.append("## Per-prompt costs")
        lines.append("")
        lines.append(f"_Sourced from `USAGE.log.jsonl` — {len(prompt_pairs)} completed prompt(s), "
                     f"total {_fmt_usd(total)}._")
        lines.append("")

        lines.append("### By agent")
        lines.append("")
        lines.append("| Agent | Prompts | Total cost | Avg / prompt |")
        lines.append("| --- | ---: | ---: | ---: |")
        for agent, s in sorted(by_agent.items(), key=lambda kv: kv[1]["cost"], reverse=True):
            avg = s["cost"] / s["count"] if s["count"] else 0.0
            lines.append(f"| {agent} | {_fmt_int(s['count'])} | {_fmt_usd(s['cost'])} | {_fmt_usd(avg)} |")
        lines.append("")

        top = sorted(prompt_pairs, key=lambda p: p["cost"], reverse=True)[:10]
        lines.append("### Top 10 most expensive prompts")
        lines.append("")
        lines.append("| Cost | Agent | Prompt id | Started | Ended |")
        lines.append("| ---: | --- | --- | --- | --- |")
        for p in top:
            lines.append(
                f"| {_fmt_usd(p['cost'])} | {p.get('agent') or '?'} | "
                f"`{p['prompt_id']}` | {p['started']} | {p['ended']} |"
            )
        lines.append("")

    # ── Account-wide activity breakdown (admin key only) ──────────────────
    lines.append("## Activity breakdown (account-wide, last 30 days)")
    lines.append("")
    lines.append(
        "_OpenRouter's `/activity` endpoint returns rollups for the entire "
        "account behind the management key — it cannot be filtered by "
        "individual API key. The headline project number above is from "
        "`/key`; the breakdown below is shown for context._"
    )
    lines.append("")

    if not admin_available:
        lines.append(
            f"_`/activity` not available: {admin_error or 'no $OPENROUTER_ADMIN_KEY set'}. "
            "Add a management/provisioning key to `~/.local/share/kilo/auth.json` "
            "as `OPENROUTER_ADMIN_KEY` to enable this section._"
        )
        lines.append("")
        return "\n".join(lines) + "\n"

    if not agg["by_day"]:
        lines.append("_No activity in the last 30 days._")
        lines.append("")
        return "\n".join(lines) + "\n"

    totals = agg["totals"]
    total_tokens = totals["prompt_tokens"] + totals["completion_tokens"]
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Total cost (account) | {_fmt_usd(totals['cost'])} |")
    lines.append(f"| Total requests | {_fmt_int(totals['requests'])} |")
    lines.append(f"| Prompt tokens | {_fmt_int(totals['prompt_tokens'])} |")
    lines.append(f"| Completion tokens | {_fmt_int(totals['completion_tokens'])} |")
    lines.append(f"| Reasoning tokens | {_fmt_int(totals['reasoning_tokens'])} |")
    lines.append(f"| Total tokens | {_fmt_int(total_tokens)} |")
    lines.append("")

    lines.append("### Daily breakdown")
    lines.append("")
    lines.append("| Date | Cost | Requests | Prompt | Completion | Reasoning |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for day, s in agg["by_day"].items():
        lines.append(
            f"| {day} | {_fmt_usd(s['cost'])} | {_fmt_int(s['requests'])} | "
            f"{_fmt_int(s['prompt_tokens'])} | {_fmt_int(s['completion_tokens'])} | "
            f"{_fmt_int(s['reasoning_tokens'])} |"
        )
    lines.append("")

    lines.append("### Model breakdown")
    lines.append("")
    lines.append("| Model | Cost | Requests | Prompt | Completion | Reasoning |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for model, s in agg["by_model"].items():
        lines.append(
            f"| `{model}` | {_fmt_usd(s['cost'])} | {_fmt_int(s['requests'])} | "
            f"{_fmt_int(s['prompt_tokens'])} | {_fmt_int(s['completion_tokens'])} | "
            f"{_fmt_int(s['reasoning_tokens'])} |"
        )
    lines.append("")

    lines.append("### Provider breakdown")
    lines.append("")
    lines.append("| Provider | Cost | Requests |")
    lines.append("| --- | ---: | ---: |")
    for provider, s in agg["by_provider"].items():
        lines.append(f"| {provider} | {_fmt_usd(s['cost'])} | {_fmt_int(s['requests'])} |")
    lines.append("")

    return "\n".join(lines) + "\n"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] usage_report: %(message)s",
    )
    log = logging.getLogger("usage_report")

    project_key, project_key_source = _resolve_project_key()
    admin_key = os.environ.get("OPENROUTER_ADMIN_KEY", "")
    project_state = _read_project_state()

    if not project_key and not admin_key:
        log.error(
            "No keys found. Set OPENROUTER_PROJECT_KEY (or drop a project-local "
            "auth.json), OPENROUTER_API_KEY, or OPENROUTER_ADMIN_KEY."
        )
        return 2

    is_global_fallback = "global fallback" in project_key_source
    title = os.environ.get("OPENROUTER_APP_TITLE") or project_state.get("name") or _DEFAULT_TITLE
    out_path = Path(os.environ.get("USAGE_REPORT_PATH") or (Path.cwd() / "USAGE.md"))

    log.info("project key source: %s", project_key_source or "(none)")
    if project_state:
        log.info("project: name=%s status=%s key_hash=%s",
                 project_state.get("name"), project_state.get("status"),
                 project_state.get("key_hash"))
    if admin_key:
        log.info("admin key present — /activity enabled")
    else:
        log.info("no admin key — /activity breakdown will be skipped")

    project_key_data: dict[str, Any] | None = None
    project_key_error = ""
    credits: dict[str, Any] | None = None

    # Authoritative path: when project_init.py provisioned a key, use the
    # admin key + /keys/{hash} (key_hash is stored in .kilo/project.json).
    # Falls through to the inference-key /key path when not available.
    used_keys_endpoint = False
    if admin_key and project_state.get("key_hash"):
        khash = project_state["key_hash"]
        log.info("fetching authoritative spend via admin /keys/%s", khash)
        keys_resp = _fetch_key_by_hash(admin_key, khash)
        if keys_resp:
            project_key_data = keys_resp
            project_key_source = f"admin /keys/{khash}"
            used_keys_endpoint = True
        else:
            project_key_error = f"admin /keys/{khash} failed"
            log.warning("falling back to inference-key /key path")

    if not used_keys_endpoint and project_key:
        with _client(project_key, title) as client:
            project_key_data, project_key_error = _get(client, "/key")
            credits, _ = _get(client, "/credits")

    activity: dict[str, Any] | None = None
    admin_error = ""
    if admin_key:
        with _client(admin_key, title) as client:
            activity, admin_error = _get(client, "/activity")
            if credits is None:
                credits, _ = _get(client, "/credits")

    rows = _activity_rows(activity)
    agg = _aggregate(rows)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())

    # Append a snapshot when /key returned valid data — skip on errors so we
    # don't pollute the trend with rows whose key_usage is 0/unknown.
    keyd = (project_key_data or {}).get("data") or {}
    cred = (credits or {}).get("data") or {}
    if isinstance(keyd.get("usage"), (int, float)):
        snapshot = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "key_label": keyd.get("label"),
            "key_usage": float(keyd["usage"]),
            "key_limit": keyd.get("limit"),
            "account_usage": cred.get("total_usage") if isinstance(cred.get("total_usage"), (int, float)) else None,
            "account_credits": cred.get("total_credits") if isinstance(cred.get("total_credits"), (int, float)) else None,
            "key_source": project_key_source,
        }
        path = _append_history(snapshot)
        log.info("appended snapshot to %s", path)
    else:
        log.info("skipping history snapshot (no valid /key.usage)")

    history = _load_history()
    history_limit = int(os.environ.get("USAGE_HISTORY_LIMIT", "10"))
    prompt_pairs = _read_prompt_log()

    body = _render(
        title=title,
        project_state=project_state,
        project_key_source=project_key_source,
        project_key_data=project_key_data,
        project_key_error=project_key_error,
        is_global_fallback=is_global_fallback,
        credits=credits,
        admin_available=activity is not None,
        admin_error=admin_error,
        agg=agg,
        history=history,
        history_limit=history_limit,
        prompt_pairs=prompt_pairs,
        generated_at=generated_at,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    log.info("wrote %s (%d bytes)", out_path, len(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
