#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
#   "httpx>=0.27.0",
# ]
# ///
"""
task-enricher MCP server.

Exposes `enrich_task(prompt, agent=None, context=None)` which calls OpenAI's
chat-completions endpoint with the model pinned at $TASK_ENRICH_MODEL (default
"gpt-5.4") and returns a structured pre-flight brief:

    {
      "objectives":     [...],   # what success looks like
      "constraints":    [...],   # repo conventions, perms, dont-touch lists
      "files_likely":   [...],   # likely files / modules to read or edit
      "risks":          [...],   # things that could break sideways
      "open_questions": [...],   # ambiguities to confirm with the user
      "summary":        "..."    # one-paragraph synthesis
    }

The dispatching agent reads the brief and uses it to plan its work. Rule 05
(`.kilo/rules/05-task-enrichment.md`) governs WHEN this tool fires; this
server only knows HOW.

Why direct OpenAI rather than OpenRouter
----------------------------------------
Users typically already pay for a ChatGPT/OpenAI subscription. Routing
gpt-5.4 through OpenRouter adds a per-token markup that delivers no value
for a pre-flight pass. We hit api.openai.com directly when OPENAI_API_KEY
is set; fall back to OpenRouter (`openrouter/openai/gpt-5.4`) when only
OPENROUTER_API_KEY is configured.

Auth
----
Reads OPENAI_API_KEY (preferred) or OPENROUTER_API_KEY from auth.json via
the shared _auth loader. Existing env vars are NEVER overridden.

Env knobs
---------
  TASK_ENRICH_MODEL          override the model id (default "gpt-5.4")
  TASK_ENRICH_MAX_TOKENS     cap on the response size (default 1024)
  TASK_ENRICH_TIMEOUT        per-call HTTP timeout in seconds (default 30)
  OPENAI_BASE_URL            override the OpenAI endpoint
  OPENROUTER_BASE_URL        override the OpenRouter endpoint
  KILO_ENRICH                set to "0" to make `enrich_task` return a
                             no-op brief without hitting the network. Useful
                             for tests and for users who want to opt out
                             without rewriting Rule 05.
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


_OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
_OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
_OR_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
_MODEL = os.environ.get("TASK_ENRICH_MODEL", "gpt-5.4")
_MAX_TOKENS = int(os.environ.get("TASK_ENRICH_MAX_TOKENS", "1024"))
_TIMEOUT = float(os.environ.get("TASK_ENRICH_TIMEOUT", "30"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] task-enricher: %(message)s",
)
log = logging.getLogger("task-enricher")

mcp = FastMCP("task-enricher")


_SYSTEM_PROMPT = """You are a pre-flight task enricher for an autonomous coding agent.

Read the user's raw task prompt and return a structured brief the downstream agent will use to plan its work. Be concrete and concise — every line should pay for itself.

Always respond with a single JSON object matching this schema EXACTLY:

{
  "objectives":     [string],   // 1-5 items; what done looks like
  "constraints":    [string],   // 0-5 items; repo conventions, permissions, no-touch areas
  "files_likely":   [string],   // 0-10 items; specific file paths or module names worth opening first
  "risks":          [string],   // 0-5 items; foot-guns or surprising failure modes
  "open_questions": [string],   // 0-3 items; ambiguities you'd ask the user before coding
  "summary":        string      // one paragraph synthesis, max 3 sentences
}

Rules:
- Output ONLY the JSON object. No markdown fences, no preamble, no trailing prose.
- If the task is trivial, return short lists — do not pad.
- If the prompt is ambiguous, surface that in open_questions rather than guessing.
- Never invent file paths you have no evidence for; leave files_likely empty if unsure.
"""


def _route() -> tuple[str, str, dict[str, str]]:
    """Pick (base_url, model_id, headers) based on which key is present."""
    if _OPENAI_KEY:
        return (
            _OPENAI_BASE,
            _MODEL,
            {
                "Authorization": f"Bearer {_OPENAI_KEY}",
                "Content-Type": "application/json",
            },
        )
    if _OR_KEY:
        # OpenRouter exposes OpenAI models under the openai/* prefix.
        model = _MODEL if "/" in _MODEL else f"openai/{_MODEL}"
        return (
            _OR_BASE,
            model,
            {
                "Authorization": f"Bearer {_OR_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kilo-org/kilo-me",
                "X-Title": "kilo-me task-enricher",
            },
        )
    return ("", "", {})


def _empty_brief(note: str) -> dict[str, Any]:
    return {
        "objectives": [],
        "constraints": [],
        "files_likely": [],
        "risks": [],
        "open_questions": [],
        "summary": note,
        "_enriched": False,
    }


def _parse_brief(content: str) -> dict[str, Any]:
    """Best-effort JSON parse — strips ``` fences if the model adds them."""
    text = content.strip()
    if text.startswith("```"):
        # Drop leading fence (optionally with language) and trailing fence.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return _empty_brief(f"parse error: {exc}; raw: {content[:300]}")
    if not isinstance(data, dict):
        return _empty_brief("model returned non-object JSON")
    # Normalize — keep the keys we promise even if the model omitted some.
    out: dict[str, Any] = {
        "objectives": list(data.get("objectives") or []),
        "constraints": list(data.get("constraints") or []),
        "files_likely": list(data.get("files_likely") or []),
        "risks": list(data.get("risks") or []),
        "open_questions": list(data.get("open_questions") or []),
        "summary": str(data.get("summary") or ""),
        "_enriched": True,
    }
    return out


@mcp.tool()
def enrich_task(
    prompt: str,
    agent: str | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """Return a structured pre-flight brief for the given task prompt.

    Args:
        prompt:  the raw user task / agent invocation text.
        agent:   optional slug of the dispatching agent (e.g. "coder-ch")
                 — included in the system prompt to bias the brief.
        context: optional extra context (repo summary, recent decisions, etc.).

    Returns:
        A dict matching the schema in this module's docstring. On any failure
        (no key, network error, parse error) returns an empty-brief shape
        with `_enriched: false` so the caller can fall through safely.
    """
    if os.environ.get("KILO_ENRICH", "1") == "0":
        return _empty_brief("KILO_ENRICH=0 — enrichment disabled by env")

    base, model, headers = _route()
    if not base:
        return _empty_brief(
            "no OPENAI_API_KEY or OPENROUTER_API_KEY available — skipping enrichment"
        )

    user_block = prompt.strip()
    if agent:
        user_block = f"Dispatching agent: {agent}\n\n{user_block}"
    if context:
        user_block = f"{user_block}\n\n---\nExtra context:\n{context.strip()}"

    body = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ],
        "response_format": {"type": "json_object"},
    }

    try:
        with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
            r = client.post(f"{base}/chat/completions", json=body)
    except httpx.HTTPError as exc:
        log.warning("enrich_task: network error: %s", exc)
        return _empty_brief(f"network error: {exc}")

    if r.status_code >= 400:
        log.warning("enrich_task: HTTP %s: %s", r.status_code, r.text[:200])
        return _empty_brief(f"HTTP {r.status_code}: {r.text[:200]}")

    try:
        payload = r.json()
    except ValueError:
        return _empty_brief("non-JSON response body")

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        return _empty_brief(f"unexpected response shape ({exc}): {payload}")

    return _parse_brief(content)


@mcp.tool()
def enrichment_status() -> dict[str, Any]:
    """Diagnostic — report which provider would be used and whether KILO_ENRICH=0.

    No network call. Useful to verify auth.json before relying on Rule 05.
    """
    base, model, _ = _route()
    return {
        "enabled": os.environ.get("KILO_ENRICH", "1") != "0",
        "provider": "openai" if _OPENAI_KEY else ("openrouter" if _OR_KEY else "none"),
        "model": model or _MODEL,
        "base_url": base,
        "has_openai_key": bool(_OPENAI_KEY),
        "has_openrouter_key": bool(_OR_KEY),
    }


if __name__ == "__main__":
    mcp.run()
