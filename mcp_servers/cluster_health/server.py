#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
cluster-health MCP server.

Exposes three tools for agents to inspect the local Ollama cluster:

  cluster_status()       — current worker health (router /healthz or direct probe)
  worker_models()        — model menu per worker
  route_for(...)         — dry-run classification: which tier would this land on?

Modes
-----
Detected by sniffing auth.json:

  router mode  — auth.json.local.base_url == auth.json["local-hard"].base_url.
                 The router (Pi 5) handles classification; we just proxy to its
                 /healthz, /v1/models, /v1/classify endpoints.

  direct mode  — distinct base URLs per provider (Phase 2). We probe each
                 worker's /api/tags directly and replicate the routing rules
                 in Python because there's no router to ask.

  none         — neither provider configured. Tools return a structured stub
                 noting cluster isn't enabled.

All tools fail soft: any HTTP/network error becomes `{"error": "..."}` rather
than raising, so agents can branch instead of dying mid-task.

Env knobs
---------
  CLUSTER_HEALTH_TIMEOUT   per-call HTTP timeout in seconds (default 5)
  KILO_LOCAL=0             treat as "no cluster configured" regardless of
                           auth.json — useful for tests / temporary opt-out
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP


# ---------------------------------------------------------------------------
# Auth — share the loader with the other MCP servers.
# ---------------------------------------------------------------------------
def _load_auth_json() -> None:
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "_auth.py",
        Path.home() / ".config" / "kilo" / "mcp_servers" / "_auth.py",
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_auth_json()

_TIMEOUT = float(os.environ.get("CLUSTER_HEALTH_TIMEOUT", "5"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] cluster-health: %(message)s",
)
log = logging.getLogger("cluster-health")

mcp = FastMCP("cluster-health")


# ---------------------------------------------------------------------------
# Auth.json sniffing — re-read every call so the user can edit it without
# bouncing the MCP daemon.
# ---------------------------------------------------------------------------
def _auth_path() -> Path:
    explicit = os.environ.get("AUTH_FILE")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "kilo" / "auth.json"


def _read_auth() -> dict[str, Any]:
    try:
        return json.loads(_auth_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _detect_mode() -> dict[str, Any]:
    """Return {'mode': 'router'|'direct'|'none', 'soft_url': str, 'hard_url': str, 'token': str}."""
    if os.environ.get("KILO_LOCAL", "1") == "0":
        return {"mode": "none", "soft_url": "", "hard_url": "", "token": "",
                "note": "KILO_LOCAL=0 — opted out via env"}
    auth = _read_auth()
    soft = (auth.get("local") or {})
    hard = (auth.get("local-hard") or {})
    soft_url = soft.get("base_url", "").rstrip("/")
    hard_url = hard.get("base_url", "").rstrip("/")
    if not soft_url and not hard_url:
        return {"mode": "none", "soft_url": "", "hard_url": "", "token": "",
                "note": "no local cluster configured in auth.json"}
    token = soft.get("key") or hard.get("key") or "kilo-local"
    if soft_url and hard_url and soft_url == hard_url:
        return {"mode": "router", "soft_url": soft_url, "hard_url": hard_url, "token": token}
    return {"mode": "direct", "soft_url": soft_url, "hard_url": hard_url, "token": token}


def _strip_v1(url: str) -> str:
    """Convert '.../v1' to '...' so we can hit Ollama's /api/tags directly."""
    return url[:-3] if url.endswith("/v1") else url


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def cluster_status() -> dict[str, Any]:
    """Return the current health of each cluster worker.

    Router mode: proxies the router's `/healthz`.
    Direct mode: probes each worker's `/api/tags` and synthesizes the same shape.

    Returns:
        {
          "mode":    "router" | "direct" | "none",
          "workers": {
            "soft": {"healthy": bool, "url": "...", "last_error": "...", "last_check": "..."},
            "hard": {"healthy": bool, "url": "...", "last_error": "...", "last_check": "..."}
          }
        }
    """
    info = _detect_mode()
    if info["mode"] == "none":
        return {"mode": "none", "workers": {}, "note": info.get("note", "")}

    headers = {"Authorization": f"Bearer {info['token']}"}

    if info["mode"] == "router":
        # The router /healthz endpoint already returns the shape we want.
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.get(f"{_strip_v1(info['soft_url'])}/healthz")
                r.raise_for_status()
                payload = r.json()
                return {"mode": "router", **payload}
        except (httpx.HTTPError, ValueError) as exc:
            return {"mode": "router", "error": f"router unreachable: {exc}", "workers": {}}

    # Direct mode — probe each worker independently.
    import time
    out: dict[str, Any] = {"mode": "direct", "workers": {}}
    for tier, url in (("soft", info["soft_url"]), ("hard", info["hard_url"])):
        if not url:
            continue
        entry: dict[str, Any] = {"url": url, "last_check": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.get(f"{_strip_v1(url)}/api/tags", headers=headers)
                entry["healthy"] = r.status_code < 500
                entry["last_error"] = "" if entry["healthy"] else f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            entry["healthy"] = False
            entry["last_error"] = str(exc)
        out["workers"][tier] = entry
    return out


@mcp.tool()
def worker_models() -> dict[str, Any]:
    """Return the model menu per worker.

    Router mode: queries `/v1/models` and groups by `owned_by` (which the
    router sets to the tier name).
    Direct mode: queries each `/api/tags` and lists what's actually loaded.

    Returns:
        {"mode": "...", "models": {"soft": [...], "hard": [...]}}
    """
    info = _detect_mode()
    if info["mode"] == "none":
        return {"mode": "none", "models": {}, "note": info.get("note", "")}

    headers = {"Authorization": f"Bearer {info['token']}"}
    out: dict[str, Any] = {"mode": info["mode"], "models": {"soft": [], "hard": []}}

    if info["mode"] == "router":
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.get(f"{info['soft_url']}/models", headers=headers)
                r.raise_for_status()
                for m in (r.json().get("data") or []):
                    tier = m.get("owned_by", "soft")
                    out["models"].setdefault(tier, []).append(m.get("id"))
        except (httpx.HTTPError, ValueError) as exc:
            out["error"] = f"router /v1/models failed: {exc}"
        return out

    # Direct mode.
    for tier, url in (("soft", info["soft_url"]), ("hard", info["hard_url"])):
        if not url:
            continue
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.get(f"{_strip_v1(url)}/api/tags", headers=headers)
                r.raise_for_status()
                tags = r.json().get("models") or []
                out["models"][tier] = [t.get("name") for t in tags if isinstance(t, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            out["models"][tier] = []
            out.setdefault("errors", {})[tier] = str(exc)
    return out


@mcp.tool()
def route_for(
    model: str,
    estimated_tokens: int = 0,
    tool_count: int = 0,
) -> dict[str, Any]:
    """Predict which tier a request would land on, without sending it.

    Use this before kicking off a long-context task to gate whether to
    summarize the prompt first or escalate to a cloud agent.

    Args:
        model:             the model slug you intend to use (e.g. "qwen3-coder:14b-instruct-q8_0")
        estimated_tokens:  rough total context size; influences hard-tier escalation
        tool_count:        number of entries in your `tools` array

    Returns:
        {
          "tier":           "soft" | "hard",
          "healthy":        bool,        # is that tier currently up?
          "fallback_tier":  str,         # who answers if `tier` is dead (empty = no fallback)
          "reason":         str          # which rule fired
        }

    In router mode this calls the router's /v1/classify (source of truth).
    In direct mode it replicates the same rules locally against worker_models().
    """
    info = _detect_mode()
    if info["mode"] == "none":
        return {"tier": "none", "healthy": False, "fallback_tier": "",
                "reason": info.get("note", "no cluster")}

    # Construct a tiny mock chat request — the router only reads model,
    # tools length, and a rough character count.
    messages_content = "a" * (estimated_tokens * 4)
    tools = [{}] * tool_count
    body = {
        "model": model,
        "messages": [{"role": "user", "content": messages_content}],
        "tools": tools,
    }

    if info["mode"] == "router":
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                r = client.post(
                    f"{info['soft_url']}/classify",
                    json=body,
                    headers={"Authorization": f"Bearer {info['token']}"},
                )
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            return {"tier": "unknown", "healthy": False, "fallback_tier": "",
                    "reason": f"router /v1/classify failed: {exc}"}

    # Direct mode — replicate the rules. Models are inferred from worker_models().
    menu = worker_models().get("models", {})
    soft_models = set(menu.get("soft") or [])
    hard_models = set(menu.get("hard") or [])
    on_soft = model in soft_models
    on_hard = model in hard_models

    tier, reason = "soft", "default (model not found on any worker)"
    if on_hard and not on_soft:
        tier, reason = "hard", "model hosted on hard tier only"
    elif on_soft and not on_hard:
        tier, reason = "soft", "model hosted on soft tier only"
    elif on_soft and on_hard:
        if estimated_tokens > 8000:
            tier, reason = "hard", "estimated tokens > 8000"
        elif tool_count > 5:
            tier, reason = "hard", "tool count > 5"
        else:
            tier, reason = "hard", "ambiguous; deterministic alphabetical pick"

    # Health check for that tier.
    status = cluster_status().get("workers", {})
    healthy = bool(status.get(tier, {}).get("healthy"))
    fallback = ""
    if not healthy:
        other = "soft" if tier == "hard" else "hard"
        if status.get(other, {}).get("healthy"):
            fallback = other

    return {"tier": tier, "healthy": healthy, "fallback_tier": fallback, "reason": reason}


if __name__ == "__main__":
    mcp.run()
