#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
#   "kuzu>=0.4.0",
# ]
# ///
"""
graph-memory MCP server.

Embedded Kuzu graph database for the relationship layer that sits on top of
the SQLite prompt log and the ChromaDB diagram index. The Memory Curator
agents write a node per task plus edges to the agent, tags, diagrams, and
promoted decisions, so future planning can traverse the graph with
`graph-memory.neighbors` / `graph-memory.cypher` instead of joining tables.

Default DB path: $XDG_CONFIG_HOME/kilo/graph.kuzu (typically
~/.config/kilo/graph.kuzu). Override via GRAPH_DB_PATH env var.

Run via `uv run server.py` — uv reads the inline PEP 723 metadata above.
Pass `--init` (without launching the MCP loop) to initialize the schema and
exit; the installer uses this for the deploy step.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, cast

import kuzu
from fastmcp import FastMCP


def _exec(conn: kuzu.Connection, query: str, params: dict[str, Any] | None = None) -> kuzu.QueryResult:
    """Single-result wrapper around Connection.execute().

    Kuzu's stub types execute() as `QueryResult | list[QueryResult]` because
    multi-statement scripts return a list. We only ever pass single statements,
    so the cast is safe and lets mypy stay --strict-clean downstream.
    """
    return cast(kuzu.QueryResult, conn.execute(query, params or {}))


def _next_row(res: kuzu.QueryResult) -> list[Any]:
    """Get the next row as a positional list.

    Kuzu's typing stubs declare `get_next()` as returning a `dict[str, Any]`,
    but the runtime returns a positional list of column values. Cast at the
    boundary so the rest of the module can index by position cleanly.
    """
    return cast("list[Any]", res.get_next())

# ---------------------------------------------------------------------------
# Setup — XDG-compliant default at $XDG_CONFIG_HOME/kilo/graph.kuzu
# ---------------------------------------------------------------------------
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
_KILO_HOME = Path(os.environ.get("KILO_HOME") or (_XDG_CONFIG_HOME / "kilo"))
_DEFAULT_DB = _KILO_HOME / "graph.kuzu"
_DB_PATH = Path(os.environ.get("GRAPH_DB_PATH", str(_DEFAULT_DB)))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] graph-memory: %(message)s",
)
log = logging.getLogger("graph-memory")

mcp = FastMCP("graph-memory")

# Node tables: (label, additional-property-spec).
# `id` is always the primary key; additional props are STRING unless noted.
_NODE_TABLES: tuple[tuple[str, str], ...] = (
    ("Prompt",   "agent STRING, model STRING, success INT64"),
    ("Agent",    "description STRING"),
    ("Tag",      "category STRING"),
    ("Diagram",  "title STRING"),
    ("Decision", "pattern_n INT64"),
    ("Pattern",  "domain STRING"),
)

# Rel tables: (rel_name, from_label, to_label).
_REL_TABLES: tuple[tuple[str, str, str], ...] = (
    ("LOGGED_BY",   "Prompt",   "Agent"),
    ("TAGGED",      "Prompt",   "Tag"),
    ("DEPICTS",     "Prompt",   "Diagram"),
    ("PROMOTED_TO", "Prompt",   "Decision"),
    ("DERIVES",     "Decision", "Pattern"),
)

_VALID_LABELS = {label for label, _ in _NODE_TABLES}
_VALID_RELS = {rel for rel, _, _ in _REL_TABLES}

_db: kuzu.Database | None = None
_conn: kuzu.Connection | None = None


def _connection() -> kuzu.Connection:
    """Return a live Kuzu connection, opening the DB and applying schema lazily."""
    global _db, _conn
    if _conn is not None:
        return _conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = kuzu.Database(str(_DB_PATH))
    _conn = kuzu.Connection(_db)
    _ensure_schema(_conn)
    return _conn


def _ensure_schema(conn: kuzu.Connection) -> None:
    """Idempotently create node + rel tables. Re-running is a no-op."""
    for label, props in _NODE_TABLES:
        ddl = f"CREATE NODE TABLE IF NOT EXISTS {label}(id STRING, {props}, PRIMARY KEY (id))"
        _exec(conn,ddl)
    for rel, src, dst in _REL_TABLES:
        ddl = f"CREATE REL TABLE IF NOT EXISTS {rel}(FROM {src} TO {dst}, properties STRING)"
        _exec(conn,ddl)
    log.info("kuzu schema ready at %s", _DB_PATH)


def _props_to_kv(props: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce caller-supplied props into the typed columns Kuzu expects.

    Anything beyond the declared columns gets dropped silently — keep the
    schema authoritative, do not let arbitrary keys leak into queries.
    """
    return props or {}


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def add_node(label: str, id: str, props: dict[str, Any] | None = None) -> dict[str, Any]:
    """Insert or update a node. Idempotent on (label, id).

    Allowed labels: Prompt, Agent, Tag, Diagram, Decision, Pattern.
    Props beyond the declared schema columns are silently dropped.
    """
    if label not in _VALID_LABELS:
        return {"created": False, "reason": f"unknown label {label!r}; allowed: {sorted(_VALID_LABELS)}"}
    if not id:
        return {"created": False, "reason": "id is required"}

    conn = _connection()
    p = _props_to_kv(props)

    if label == "Prompt":
        _exec(conn,
            "MERGE (n:Prompt {id: $id}) "
            "SET n.agent = $agent, n.model = $model, n.success = $success",
            {"id": id,
             "agent": str(p.get("agent", "")),
             "model": str(p.get("model", "")),
             "success": int(p.get("success") or 0)},
        )
    elif label == "Agent":
        _exec(conn,
            "MERGE (n:Agent {id: $id}) SET n.description = $description",
            {"id": id, "description": str(p.get("description", ""))},
        )
    elif label == "Tag":
        _exec(conn,
            "MERGE (n:Tag {id: $id}) SET n.category = $category",
            {"id": id, "category": str(p.get("category", ""))},
        )
    elif label == "Diagram":
        _exec(conn,
            "MERGE (n:Diagram {id: $id}) SET n.title = $title",
            {"id": id, "title": str(p.get("title", ""))},
        )
    elif label == "Decision":
        _exec(conn,
            "MERGE (n:Decision {id: $id}) SET n.pattern_n = $pattern_n",
            {"id": id, "pattern_n": int(p.get("pattern_n") or 0)},
        )
    elif label == "Pattern":
        _exec(conn,
            "MERGE (n:Pattern {id: $id}) SET n.domain = $domain",
            {"id": id, "domain": str(p.get("domain", ""))},
        )

    log.info("upserted %s %s", label, id)
    return {"created": True, "label": label, "id": id}


@mcp.tool()
def add_edge(
    from_id: str,
    to_id: str,
    rel: str,
    props: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a relationship between two existing nodes.

    Allowed rels: LOGGED_BY, TAGGED, DEPICTS, PROMOTED_TO, DERIVES. The
    endpoint labels are derived from the rel — caller supplies ids only.
    `props` is serialized into a single STRING column (`properties`) so
    arbitrary metadata travels with the edge without schema churn.
    """
    if rel not in _VALID_RELS:
        return {"created": False, "reason": f"unknown rel {rel!r}; allowed: {sorted(_VALID_RELS)}"}

    src_label, dst_label = next((s, d) for r, s, d in _REL_TABLES if r == rel)
    conn = _connection()
    import json as _json
    props_str = _json.dumps(props or {}, sort_keys=True)

    _exec(conn,
        f"MATCH (a:{src_label} {{id: $from_id}}), (b:{dst_label} {{id: $to_id}}) "
        f"CREATE (a)-[r:{rel} {{properties: $props}}]->(b)",
        {"from_id": from_id, "to_id": to_id, "props": props_str},
    )
    log.info("edge %s: %s -[%s]-> %s", rel, from_id, rel, to_id)
    return {"created": True, "rel": rel, "from": from_id, "to": to_id}


@mcp.tool()
def neighbors(id: str, depth: int = 1) -> list[dict[str, Any]]:
    """Return nodes within `depth` hops of the given node id.

    Depth is clamped to [1, 4] to keep query cost bounded. Each result is
    `{id, label, hops, via_rel}` where via_rel names the edge type traversed
    to reach it (only meaningful for direct neighbors at depth=1).
    """
    depth = max(1, min(int(depth), 4))
    conn = _connection()
    res = _exec(conn,
        f"MATCH (a {{id: $id}})-[r*1..{depth}]-(b) "
        f"RETURN DISTINCT b.id AS id, label(b) AS label, length(r) AS hops",
        {"id": id},
    )
    out: list[dict[str, Any]] = []
    while res.has_next():
        row = _next_row(res)
        out.append({"id": row[0], "label": row[1], "hops": row[2]})
    return out


@mcp.tool()
def find_path(from_id: str, to_id: str, max_hops: int = 4) -> list[dict[str, Any]]:
    """Return the shortest path of length ≤ max_hops between two node ids.

    Empty list = no path within the bound. Each step is `{id, label}` in
    traversal order; the source and destination are included.
    """
    max_hops = max(1, min(int(max_hops), 6))
    conn = _connection()
    res = _exec(conn,
        f"MATCH p = (a {{id: $from_id}})-[*1..{max_hops}]-(b {{id: $to_id}}) "
        f"RETURN nodes(p) AS path ORDER BY length(p) LIMIT 1",
        {"from_id": from_id, "to_id": to_id},
    )
    if not res.has_next():
        return []
    nodes = cast("list[dict[str, Any]]", _next_row(res)[0])
    return [{"id": n["id"], "label": n["_label"]} for n in nodes]


@mcp.tool()
def cypher(query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
    """Run an arbitrary read-only Cypher query and return the rows.

    Use sparingly — prefer `neighbors` / `find_path` when the question fits
    those shapes. Write queries (CREATE/SET/DELETE/MERGE) are NOT blocked at
    this layer — only run trusted Cypher here.
    """
    conn = _connection()
    res = _exec(conn,query, params or {})
    rows: list[list[Any]] = []
    while res.has_next():
        rows.append(_next_row(res))
    return rows


@mcp.tool()
def stats() -> dict[str, Any]:
    """Diagnostic: per-label node counts and per-rel edge counts."""
    conn = _connection()
    out: dict[str, Any] = {"path": str(_DB_PATH), "nodes": {}, "edges": {}}
    for label, _ in _NODE_TABLES:
        res = _exec(conn, f"MATCH (n:{label}) RETURN COUNT(n)")
        out["nodes"][label] = _next_row(res)[0] if res.has_next() else 0
    for rel, src, dst in _REL_TABLES:
        res = _exec(conn, f"MATCH (:{src})-[r:{rel}]->(:{dst}) RETURN COUNT(r)")
        out["edges"][rel] = _next_row(res)[0] if res.has_next() else 0
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if "--init" in sys.argv:
        # Schema-only mode for install.sh — open the DB, ensure schema, exit.
        _connection()
        log.info("graph-memory init complete; db=%s", _DB_PATH)
        sys.exit(0)
    log.info("graph-memory MCP server starting; db=%s", _DB_PATH)
    mcp.run()
