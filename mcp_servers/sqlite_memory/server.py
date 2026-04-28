#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
# ]
# ///
"""
sqlite-memory MCP server.

Persists every agent prompt + outcome to a local SQLite database with FTS5
full-text search. Run via `uv run server.py` — uv reads the inline PEP 723
metadata above and creates an ephemeral venv automatically.

Default DB path: $XDG_CONFIG_HOME/kilo/memory.sqlite (typically
~/.config/kilo/memory.sqlite). Override via MEMORY_DB_PATH env var.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Setup — XDG-compliant default at $XDG_CONFIG_HOME/kilo/memory.sqlite
# (typically ~/.config/kilo/memory.sqlite). Override via MEMORY_DB_PATH.
# ---------------------------------------------------------------------------
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
_KILO_HOME = Path(os.environ.get("KILO_HOME") or (_XDG_CONFIG_HOME / "kilo"))
_DEFAULT_DB = _KILO_HOME / "memory.sqlite"
_DB_PATH = Path(os.environ.get("MEMORY_DB_PATH", str(_DEFAULT_DB)))
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] sqlite-memory: %(message)s",
)
log = logging.getLogger("sqlite-memory")

mcp = FastMCP("sqlite-memory")


def _ensure_schema() -> None:
    """Create the DB file and apply schema.sql if needed."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(_DB_PATH) as c:
        if _SCHEMA_PATH.exists():
            c.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        else:
            log.warning("schema.sql not found at %s — using minimal schema", _SCHEMA_PATH)
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS prompts(
                    id TEXT PRIMARY KEY, session_id TEXT, ts TEXT, agent TEXT,
                    model TEXT, prompt TEXT, completion TEXT, tokens_in INT,
                    tokens_out INT, success INT, tags TEXT, mermaid_id TEXT,
                    cost_usd REAL DEFAULT 0
                );
                """
            )


@contextmanager
def _conn():
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _utcnow() -> _dt.datetime:
    """Module-level UTC clock — monkeypatch this in tests."""
    return _dt.datetime.now(_dt.timezone.utc)


def _hash_prompt(agent: str, prompt: str) -> str:
    """Stable 12-char ID derived from agent + prompt content + day."""
    day = _utcnow().strftime("%Y%m%d")
    h = hashlib.sha1(f"{agent}|{day}|{prompt}".encode("utf-8")).hexdigest()
    return h[:12]


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def save_prompt(
    agent: str,
    model: str,
    prompt: str,
    completion: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    success: bool | None = None,
    tags: list[str] | None = None,
    mermaid_id: str | None = None,
    cost_usd: float = 0.0,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Insert or update a prompt record. Returns the prompt id.

    Idempotent on (agent, prompt, day): repeated calls update the same row,
    which is how Rule 02 (start + end logging) works.
    """
    pid = _hash_prompt(agent, prompt)
    ts = _utcnow().isoformat(timespec="seconds")
    tags_json = json.dumps(tags or [])
    success_int: int | None = None if success is None else (1 if success else 0)

    with _conn() as c:
        existing = c.execute("SELECT id FROM prompts WHERE id = ?", (pid,)).fetchone()
        if existing:
            c.execute(
                """
                UPDATE prompts
                SET completion = ?, tokens_in = ?, tokens_out = ?,
                    success = ?, tags = ?, mermaid_id = COALESCE(?, mermaid_id),
                    cost_usd = ?, model = ?
                WHERE id = ?
                """,
                (completion, tokens_in, tokens_out, success_int, tags_json,
                 mermaid_id, cost_usd, model, pid),
            )
            log.info("updated prompt %s (success=%s)", pid, success_int)
        else:
            c.execute(
                """
                INSERT INTO prompts
                  (id, session_id, ts, agent, model, prompt, completion,
                   tokens_in, tokens_out, success, tags, mermaid_id, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (pid, session_id, ts, agent, model, prompt, completion,
                 tokens_in, tokens_out, success_int, tags_json, mermaid_id, cost_usd),
            )
            log.info("inserted prompt %s (agent=%s)", pid, agent)

    return {"id": pid, "ts": ts, "updated": bool(existing)}


@mcp.tool()
def search_prompts(
    query: str,
    limit: int = 5,
    only_successful: bool = False,
    agent: str | None = None,
) -> list[dict[str, Any]]:
    """Full-text search over prompt bodies, completions, and tags."""
    sql = (
        "SELECT p.id, p.ts, p.agent, p.model, p.prompt, p.completion, "
        "       p.tokens_in, p.tokens_out, p.success, p.tags, p.mermaid_id "
        "FROM prompts p JOIN prompts_fts f ON p.rowid = f.rowid "
        "WHERE prompts_fts MATCH ?"
    )
    params: list[Any] = [query]
    if only_successful:
        sql += " AND p.success = 1"
    if agent:
        sql += " AND p.agent = ?"
        params.append(agent)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    return [
        {
            "id": r["id"], "ts": r["ts"], "agent": r["agent"], "model": r["model"],
            "prompt": r["prompt"], "completion": r["completion"],
            "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"],
            "success": bool(r["success"]) if r["success"] is not None else None,
            "tags": json.loads(r["tags"] or "[]"),
            "mermaid_id": r["mermaid_id"],
        }
        for r in rows
    ]


@mcp.tool()
def get_session_history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return all prompts in a session, oldest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts, agent, model, prompt, completion, success, tags "
            "FROM prompts WHERE session_id = ? ORDER BY ts ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


@mcp.tool()
def count_pattern(domain_tag: str, only_successful: bool = True) -> dict[str, Any]:
    """Count prompts whose tags include the given domain tag.

    Used by the Memory Curator to decide if a pattern should be promoted.
    """
    sql = "SELECT id, ts FROM prompts WHERE tags LIKE ?"
    params: list[Any] = [f'%"{domain_tag}"%']
    if only_successful:
        sql += " AND success = 1"
    sql += " ORDER BY ts ASC"
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    if not rows:
        return {"count": 0, "first_seen": None, "last_seen": None, "ids": []}

    distinct_days = {r["ts"][:10] for r in rows}
    return {
        "count": len(rows),
        "first_seen": rows[0]["ts"],
        "last_seen": rows[-1]["ts"],
        "distinct_days": len(distinct_days),
        "ids": [r["id"] for r in rows],
        "promotion_ready": len(rows) >= 3 and len(distinct_days) >= 2,
    }


@mcp.tool()
def record_promotion(
    title: str,
    domain: str,
    prompt_ids: list[str],
    mermaid_ids: list[str] | None = None,
    repo_path: str | None = None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Log a successful promotion to the best-practices repo."""
    pid = hashlib.sha1(f"{title}|{_utcnow()}".encode()).hexdigest()[:12]
    with _conn() as c:
        c.execute(
            """
            INSERT INTO promotions
              (id, promoted_at, title, domain, pattern_n, prompt_ids,
               mermaid_ids, repo_path, pr_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pid, _utcnow().isoformat(timespec="seconds"),
             title, domain, len(prompt_ids), json.dumps(prompt_ids),
             json.dumps(mermaid_ids or []), repo_path, pr_url),
        )
    return {"id": pid, "title": title, "pattern_n": len(prompt_ids)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _ensure_schema()
    log.info("sqlite-memory MCP server starting; db=%s", _DB_PATH)
    mcp.run()
