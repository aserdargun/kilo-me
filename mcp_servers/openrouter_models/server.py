#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
openrouter-models MCP server.

Maintains an auto-refreshed catalog of OpenRouter models. By default the
catalog spans the FULL OpenRouter model list — no category filter — so any
model is reachable through `model_for_budget`. The scoring algorithm naturally
surfaces models with tool-calling support and competitive pricing toward the
top, while penalizing models that lack tool-calling (a hard requirement for
agentic coding).

Default cache path: $XDG_CONFIG_HOME/kilo/models.curated.json (typically
~/.config/kilo/models.curated.json). Override via MODELS_CACHE_PATH.

Credentials: reads OPENROUTER_API_KEY from $XDG_DATA_HOME/kilo/auth.json
(typically ~/.local/share/kilo/auth.json) if not already set in the env.
Override the auth file location via $AUTH_FILE.

Run via `uv run server.py` — uv reads the inline PEP 723 metadata and
materializes deps automatically.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Credentials — load auth.json (~/.local/share/kilo/auth.json by default)
# before any os.environ read. Existing env vars are NEVER overridden.
# ---------------------------------------------------------------------------
def _load_auth_json() -> None:
    """Best-effort load of the shared auth.json loader from a sibling path."""
    import importlib.util
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent / "_auth.py",                                 # repo / install layout
        Path.home() / ".config" / "kilo" / "mcp_servers" / "_auth.py",  # explicit fallback
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_auth_json()

# ---------------------------------------------------------------------------
# Setup — XDG-compliant default at $XDG_CONFIG_HOME/kilo/models.curated.json
# ---------------------------------------------------------------------------
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
_KILO_HOME = Path(os.environ.get("KILO_HOME") or (_XDG_CONFIG_HOME / "kilo"))
_DEFAULT_CACHE = _KILO_HOME / "models.curated.json"
_OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_CACHE_PATH = Path(os.environ.get("MODELS_CACHE_PATH", str(_DEFAULT_CACHE)))
_TTL_SECONDS = int(os.environ.get("MODELS_TTL_SECONDS", "86400"))  # 24h
_OR_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Fallback chain file — user-editable per-agent list of {primary, alt1..alt4}.
# Default lives next to kilo.jsonc; override for tests.
_FALLBACKS_PATH = Path(os.environ.get("FALLBACKS_PATH", str(_KILO_HOME / "fallbacks.json")))
# Optional category filter — leave empty to fetch ALL models. Examples:
# "programming", "roleplay", "vision", "translation". Comma-separated list OK.
_CATEGORY = os.environ.get("OPENROUTER_CATEGORY", "")

# Authors we treat as "Chinese frontier" — earns the model a scoring bonus.
_CHINESE_AUTHORS = (
    "moonshotai",
    "deepseek",
    "z-ai",
    "qwen",
    "alibaba",
    "xiaomi",
    "minimax",
    "stepfun",
    "01-ai",
    "baichuan",
    "zhipu",
    "tencent",
)

# Models always included in the curated cache, regardless of score. Use this
# to guarantee strategic models survive the top_n cutoff even when newer
# pricing pushes them below cheaper-but-weaker peers.
_PINNED_MODELS = (
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "z-ai/glm-5.1",
    "qwen/qwen3.6-plus",
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] openrouter-models: %(message)s",
)
log = logging.getLogger("openrouter-models")

mcp = FastMCP("openrouter-models")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _is_chinese(model_id: str) -> bool:
    return any(model_id.lower().startswith(a + "/") for a in _CHINESE_AUTHORS)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _score(model: dict[str, Any]) -> float:
    """Weighted score: Chinese bias + tool calling + cheap > expensive.

    Tool-calling is non-negotiable for agentic coding — its absence is a
    heavy penalty rather than just a missing bonus.
    """
    score = 0.0
    mid = model.get("id", "")

    # 1) Chinese frontier bias
    if _is_chinese(mid):
        score += 30.0

    # 2) Tool-calling support is mandatory for agentic coding
    params = model.get("supported_parameters") or []
    if "tools" in params or "tool_choice" in params:
        score += 15.0
    else:
        score -= 25.0

    # 3) Long context — 200k+ earns full bonus, 128k+ partial
    ctx = int(model.get("context_length") or 0)
    if ctx >= 1_000_000:
        score += 15.0
    elif ctx >= 200_000:
        score += 10.0
    elif ctx >= 128_000:
        score += 5.0

    # 4) Cost — convert per-token to per-million-tokens, then reward cheap
    pricing = model.get("pricing", {}) or {}
    completion_per_tok = _safe_float(pricing.get("completion"))
    completion_per_mtok = completion_per_tok * 1_000_000
    score += max(0.0, 20.0 - completion_per_mtok)

    # 5) Reasoning-capable models edge ahead for debug agent
    if "reasoning" in params:
        score += 5.0

    return score


def _fetch_models(category: str | None = None) -> list[dict[str, Any]]:
    """Hit OpenRouter /models. With category=None, returns the full catalog."""
    if not _OR_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    headers = {"Authorization": f"Bearer {_OR_KEY}"}
    url = f"{_OR_BASE}/models"
    params: dict[str, Any] = {}
    effective_cat = category if category is not None else _CATEGORY
    if effective_cat:
        params["category"] = effective_cat
    log.info("fetching %s%s", url, f"?category={effective_cat}" if effective_cat else " (ALL)")
    with httpx.Client(timeout=20.0) as client:
        r = client.get(url, params=params or None, headers=headers)
        r.raise_for_status()
        data = r.json()
    return data.get("data", [])


def _load_cache() -> dict[str, Any] | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("cache read failed: %s", exc)
        return None


def _save_cache(payload: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _cache_is_fresh(cache: dict[str, Any] | None) -> bool:
    if not cache:
        return False
    return (time.time() - cache.get("updated", 0)) < _TTL_SECONDS


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def refresh_catalog(top_n: int = 30, category: str | None = None) -> dict[str, Any]:
    """Force-refresh the cached model catalog from OpenRouter.

    Args:
        top_n: how many top-scored models to keep in the cache (default 30).
        category: optional OpenRouter category filter. None = fetch all models.

    Returns a summary including the top model IDs by score.
    """
    models = _fetch_models(category=category)
    if not models:
        log.warning("OpenRouter returned 0 models — keeping previous cache")
        return {"refreshed": False, "count": 0}

    all_scored = sorted(
        ({**m, "_score": _score(m)} for m in models),
        key=lambda m: m["_score"],
        reverse=True,
    )
    top = all_scored[:top_n]
    top_ids = {m["id"] for m in top}
    pinned_extras = [m for m in all_scored if m["id"] in _PINNED_MODELS and m["id"] not in top_ids]
    if pinned_extras:
        log.info("including %d pinned model(s) below top_n cutoff: %s",
                 len(pinned_extras), [m["id"] for m in pinned_extras])
    scored = top + pinned_extras

    payload = {
        "updated": time.time(),
        "updated_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": f"{_OR_BASE}/models" + (f"?category={category}" if category else ""),
        "total_available": len(models),
        "weights": {
            "chinese_bias": 30,
            "tool_calling_required": 15,
            "missing_tools_penalty": -25,
            "long_context": "5/10/15",
            "cost_band": "0-20",
            "reasoning_bonus": 5,
        },
        "models": scored,
    }
    _save_cache(payload)
    log.info(
        "refreshed: %d/%d models cached; top = %s (score=%.1f)",
        len(scored), len(models), scored[0]["id"], scored[0]["_score"],
    )
    return {
        "refreshed": True,
        "count": len(scored),
        "total_available": len(models),
        "top": [{"id": m["id"], "score": round(m["_score"], 1)} for m in scored[:10]],
    }


@mcp.tool()
def top_coding_models(n: int = 5, chinese_only: bool = False) -> list[dict[str, Any]]:
    """Return the top N coding-suitable models from the cached catalog.

    "Coding-suitable" here means tool-calling support — without that, an agent
    can't do meaningful work, regardless of benchmark scores.
    """
    cache = _load_cache()
    if not _cache_is_fresh(cache):
        log.info("cache stale or missing — refreshing")
        refresh_catalog()
        cache = _load_cache() or {"models": []}

    out = []
    for m in cache.get("models", []):
        params = m.get("supported_parameters") or []
        if "tools" not in params and "tool_choice" not in params:
            continue
        if chinese_only and not _is_chinese(m["id"]):
            continue
        pricing = m.get("pricing", {}) or {}
        out.append({
            "id": m["id"],
            "score": round(m.get("_score", 0.0), 1),
            "context_length": m.get("context_length"),
            "completion_per_mtok_usd": round(_safe_float(pricing.get("completion")) * 1_000_000, 2),
            "supports_tools": True,
            "supports_reasoning": "reasoning" in params,
            "is_chinese": _is_chinese(m["id"]),
        })
        if len(out) >= n:
            break
    return out


def _is_free(model: dict[str, Any]) -> bool:
    """A model is free if both prompt and completion prices are 0, or its id ends with `:free`."""
    if model.get("id", "").endswith(":free"):
        return True
    pricing = model.get("pricing", {}) or {}
    return (
        _safe_float(pricing.get("prompt")) == 0.0
        and _safe_float(pricing.get("completion")) == 0.0
    )


@mcp.tool()
def top_free_models(limit: int = 5, require_tool_calls: bool = True) -> list[dict[str, Any]]:
    """Return free OpenRouter models, ranked by the same score used elsewhere.

    A model qualifies as "free" when both prompt and completion prices are 0
    OR its id has the `:free` suffix. By default we also require tool-calling
    support — without it the model can't drive an agent loop. Set
    require_tool_calls=False to include reasoning-only free models (useful
    for read-only `ask` style usage that doesn't need tool dispatch).

    Returns an ordered list. Empty if no free model meets the constraints —
    callers (the `ask` agent in particular) must handle the empty case rather
    than silently fall back to a paid model.
    """
    cache = _load_cache()
    if not _cache_is_fresh(cache):
        log.info("cache stale or missing — refreshing for top_free_models")
        refresh_catalog()
        cache = _load_cache() or {"models": []}

    out: list[dict[str, Any]] = []
    for m in cache.get("models", []):
        if not _is_free(m):
            continue
        params = m.get("supported_parameters") or []
        supports_tools = "tools" in params or "tool_choice" in params
        if require_tool_calls and not supports_tools:
            continue
        out.append({
            "id": m["id"],
            "score": round(m.get("_score", 0.0), 1),
            "context_length": m.get("context_length"),
            "supports_tools": supports_tools,
            "supports_reasoning": "reasoning" in params,
            "is_chinese": _is_chinese(m["id"]),
        })
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def model_for_budget(
    task_kind: str = "code",
    max_cost_per_mtok: float = 5.0,
    require_chinese: bool = False,
) -> dict[str, Any]:
    """Pick the best model whose completion price is <= max_cost_per_mtok USD.

    task_kind hints: "code" (default), "plan", "debug", "long_context".
    """
    cache = _load_cache()
    if not _cache_is_fresh(cache):
        refresh_catalog()
        cache = _load_cache() or {"models": []}

    candidates = list(cache.get("models", []))

    def task_bonus(m: dict[str, Any]) -> float:
        params = m.get("supported_parameters") or []
        ctx = int(m.get("context_length") or 0)
        b = 0.0
        if task_kind == "plan" and "reasoning" in params:
            b += 8.0
        if task_kind == "debug" and "reasoning" in params:
            b += 6.0
        if task_kind == "long_context" and ctx >= 500_000:
            b += 10.0
        return b

    candidates.sort(key=lambda m: m.get("_score", 0.0) + task_bonus(m), reverse=True)

    for m in candidates:
        params = m.get("supported_parameters") or []
        if "tools" not in params and "tool_choice" not in params:
            continue
        pricing = m.get("pricing", {}) or {}
        per_mtok = _safe_float(pricing.get("completion")) * 1_000_000
        if per_mtok > max_cost_per_mtok:
            continue
        if require_chinese and not _is_chinese(m["id"]):
            continue
        return {
            "id": m["id"],
            "completion_per_mtok_usd": round(per_mtok, 2),
            "context_length": m.get("context_length"),
            "task_kind": task_kind,
            "score": round(m.get("_score", 0.0) + task_bonus(m), 1),
        }

    fallback = candidates[0] if candidates else {"id": "deepseek/deepseek-v4-flash"}
    log.warning("no model met budget %.2f $/Mtok — falling back to %s",
                max_cost_per_mtok, fallback.get("id"))
    return {
        "id": fallback["id"],
        "fallback": True,
        "reason": f"No candidate <= {max_cost_per_mtok} $/Mtok",
    }


@mcp.tool()
def list_models_by_author(author: str, limit: int = 20) -> list[dict[str, Any]]:
    """List all cached models from a given author/organization (e.g., 'qwen').

    Useful when you want to see, say, every Qwen model OpenRouter currently
    routes to.
    """
    cache = _load_cache()
    if not _cache_is_fresh(cache):
        refresh_catalog()
        cache = _load_cache() or {"models": []}
    author_lower = author.lower().rstrip("/")
    out = []
    for m in cache.get("models", []):
        if m["id"].lower().startswith(author_lower + "/"):
            pricing = m.get("pricing", {}) or {}
            out.append({
                "id": m["id"],
                "score": round(m.get("_score", 0.0), 1),
                "context_length": m.get("context_length"),
                "completion_per_mtok_usd": round(
                    _safe_float(pricing.get("completion")) * 1_000_000, 2
                ),
            })
        if len(out) >= limit:
            break
    return out


@mcp.tool()
def cache_status() -> dict[str, Any]:
    """Diagnostic: where is the cache, when was it updated, how many models?"""
    cache = _load_cache()
    if not cache:
        return {"present": False, "path": str(_CACHE_PATH)}
    age = time.time() - cache.get("updated", 0)
    return {
        "present": True,
        "path": str(_CACHE_PATH),
        "updated_iso": cache.get("updated_iso"),
        "age_seconds": int(age),
        "fresh": age < _TTL_SECONDS,
        "model_count": len(cache.get("models", [])),
        "total_available": cache.get("total_available"),
        "source": cache.get("source"),
    }


# ---------------------------------------------------------------------------
# Fallback chains — Rule 06. Each agent has up to 5 model slugs ordered by
# preference. pick_fallback returns the next viable option, skipping models
# the caller has already tried. The implementation is intentionally simple:
# it does NOT probe OpenRouter for live availability (that would defeat the
# point — fallback is invoked precisely when the network is misbehaving). It
# DOES cross-reference the curated cache to skip slugs that disappeared from
# the catalog entirely (renamed / deprecated).
# ---------------------------------------------------------------------------
def _load_fallbacks() -> dict[str, list[str]]:
    """Read fallbacks.json. Returns {} on any error so callers fail soft."""
    if not _FALLBACKS_PATH.is_file():
        return {}
    try:
        data = json.loads(_FALLBACKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", _FALLBACKS_PATH, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for agent, chain in data.items():
        if agent.startswith("_"):
            continue  # _comment, _schema, etc.
        if isinstance(chain, list) and all(isinstance(s, str) for s in chain):
            out[agent] = chain
    return out


def _catalog_slugs() -> set[str]:
    """OpenRouter ids present in the curated cache (for availability filter)."""
    cache = _load_cache()
    if not cache:
        return set()
    return {m.get("id", "") for m in cache.get("models", []) if isinstance(m, dict)}


def _strip_provider(slug: str) -> str:
    """'openrouter/deepseek/deepseek-v4-pro' → 'deepseek/deepseek-v4-pro'."""
    # Strip a single leading provider segment ("openrouter/" or "openai/").
    if slug.startswith("openrouter/"):
        return slug[len("openrouter/"):]
    if slug.startswith("openai/"):
        return slug[len("openai/"):]
    return slug


@mcp.tool()
def list_fallbacks(agent: str | None = None) -> dict[str, Any]:
    """Return the configured fallback chain for one agent, or every agent.

    Args:
        agent: optional slug (e.g. "coder-ch"). If omitted, returns all chains.

    Returns:
        {agent: [model_slug, ...]} mapping, plus a "path" field showing where
        the file lives.
    """
    chains = _load_fallbacks()
    if agent:
        return {"path": str(_FALLBACKS_PATH), agent: chains.get(agent, [])}
    return {"path": str(_FALLBACKS_PATH), "chains": chains}


@mcp.tool()
def pick_fallback(
    agent: str,
    exclude: list[str] | None = None,
    skip_unavailable: bool = True,
) -> dict[str, Any]:
    """Return the next model the agent should try after a hard failure.

    Walks the agent's fallback chain in order, skipping any slug present in
    `exclude` (i.e. already attempted this turn). When `skip_unavailable` is
    true (default), also skips OpenRouter slugs that aren't in the curated
    cache — handles deprecated / renamed models gracefully.

    Args:
        agent:             agent slug (e.g. "coder-ch").
        exclude:           model slugs already tried this turn.
        skip_unavailable:  set False to skip the catalog cross-check (use when
                           the curated cache itself is stale).

    Returns:
        {
          "model":     "<slug>" | null,   # the slug to switch to (null = exhausted)
          "remaining": int,               # how many fallbacks are still untried
          "tried":     [...],             # exclude echoed back
          "chain":     [...],             # full chain for transparency
          "reason":    str                # "ok" | "exhausted" | "no-chain"
        }
    """
    chains = _load_fallbacks()
    chain = chains.get(agent) or []
    if not chain:
        return {
            "model": None,
            "remaining": 0,
            "tried": list(exclude or []),
            "chain": [],
            "reason": "no-chain",
        }

    tried = set(exclude or [])
    available_or = _catalog_slugs() if skip_unavailable else set()

    for slug in chain:
        if slug in tried:
            continue
        if skip_unavailable and slug.startswith("openrouter/"):
            # Cross-check against curated catalog. Skip if missing — but only
            # when we actually have a cache; an empty set means "unknown".
            if available_or and _strip_provider(slug) not in available_or:
                continue
        # Found a viable next pick. Compute remaining as untried entries
        # below this one in the chain.
        idx = chain.index(slug)
        remaining = sum(
            1 for s in chain[idx + 1 :]
            if s not in tried
            and not (skip_unavailable and s.startswith("openrouter/")
                     and available_or and _strip_provider(s) not in available_or)
        )
        return {
            "model": slug,
            "remaining": remaining,
            "tried": list(exclude or []),
            "chain": chain,
            "reason": "ok",
        }

    return {
        "model": None,
        "remaining": 0,
        "tried": list(exclude or []),
        "chain": chain,
        "reason": "exhausted",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info(
        "openrouter-models MCP server starting; cache=%s ttl=%ds category=%s",
        _CACHE_PATH, _TTL_SECONDS, _CATEGORY or "ALL",
    )
    mcp.run()
