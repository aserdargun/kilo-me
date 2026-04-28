#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp>=0.4.0",
#   "chromadb>=0.5.0",
#   "sentence-transformers>=2.7.0",
# ]
# ///
"""
mermaid-vector MCP server.

Embeds Mermaid diagrams (the source text of every architectural decision)
into a local ChromaDB collection so the agents can semantically recall prior
designs.

Default Chroma path: $XDG_CONFIG_HOME/kilo/chroma (typically ~/.config/kilo/chroma).
Override via CHROMA_PATH env var.

Run via `uv run server.py` — the first invocation pulls chromadb +
sentence-transformers into uv's cache, which takes a minute. Subsequent runs
are fast.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any

import chromadb
from chromadb.utils import embedding_functions
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Setup — XDG-compliant default at $XDG_CONFIG_HOME/kilo/chroma
# ---------------------------------------------------------------------------
_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
_KILO_HOME = Path(os.environ.get("KILO_HOME") or (_XDG_CONFIG_HOME / "kilo"))
_DEFAULT_CHROMA = _KILO_HOME / "chroma"
_CHROMA_PATH = Path(os.environ.get("CHROMA_PATH", str(_DEFAULT_CHROMA)))
_COLLECTION = os.environ.get("CHROMA_COLLECTION", "decisions")
_EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] mermaid-vector: %(message)s",
)
log = logging.getLogger("mermaid-vector")

mcp = FastMCP("mermaid-vector")

_client: chromadb.api.ClientAPI | None = None
_collection: Any = None


def _get_collection() -> Any:
    global _client, _collection
    if _collection is not None:
        return _collection
    _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(_CHROMA_PATH))
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=_EMBED_MODEL)
    _collection = _client.get_or_create_collection(
        name=_COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    log.info("chromadb collection '%s' opened (%s)", _COLLECTION, _CHROMA_PATH)
    return _collection


_VALID_DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "stateDiagram", "stateDiagram-v2",
    "classDiagram", "erDiagram", "gantt", "journey", "gitGraph", "mindmap",
    "timeline", "quadrantChart", "C4Context",
)


def _validate_mermaid(diagram: str) -> tuple[bool, str]:
    """Light syntax check: must start with a known diagram keyword."""
    stripped = diagram.strip()
    if not stripped:
        return False, "empty diagram"
    body = "\n".join(line for line in stripped.splitlines()
                     if not line.strip().startswith("%%"))
    body = body.strip()
    first_token = re.split(r"\s", body, maxsplit=1)[0] if body else ""
    if first_token not in _VALID_DIAGRAM_TYPES:
        return False, f"unrecognized diagram type: {first_token!r}"
    return True, "ok"


def _diagram_id(diagram: str) -> str:
    return hashlib.sha1(diagram.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------
@mcp.tool()
def ingest_mermaid(
    diagram: str,
    title: str,
    tags: list[str] | None = None,
    sqlite_prompt_id: str | None = None,
) -> dict[str, Any]:
    """Embed a Mermaid diagram and store it in ChromaDB.

    Idempotent on diagram content: same diagram text yields the same id.
    Re-ingesting updates metadata (title, tags) without re-embedding.
    """
    ok, reason = _validate_mermaid(diagram)
    if not ok:
        return {"ingested": False, "reason": reason}

    did = _diagram_id(diagram)
    tags_list = tags or []
    tag_str = ",".join(tags_list)
    metadata = {
        "title": title,
        "tags": tag_str,
        "raw": diagram,
        "ingested_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "sqlite_prompt_id": sqlite_prompt_id or "",
    }
    document = f"{title}\n[{tag_str}]\n{diagram}"

    coll = _get_collection()
    coll.upsert(ids=[did], documents=[document], metadatas=[metadata])
    log.info("ingested mermaid %s '%s' (%d tags)", did, title, len(tags_list))
    return {"ingested": True, "id": did, "title": title}


@mcp.tool()
def search_diagrams(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Semantic search across stored Mermaid diagrams.

    Returns a list of {id, title, tags, distance, diagram} dicts ordered by
    cosine similarity (lower distance = more similar).
    """
    coll = _get_collection()
    if coll.count() == 0:
        return []
    res = coll.query(query_texts=[query], n_results=min(k, coll.count()))
    out: list[dict[str, Any]] = []
    for did, meta, dist in zip(
        res["ids"][0], res["metadatas"][0], res["distances"][0]
    ):
        out.append({
            "id": did,
            "title": meta.get("title", ""),
            "tags": (meta.get("tags") or "").split(",") if meta.get("tags") else [],
            "distance": round(float(dist), 4),
            "diagram": meta.get("raw", ""),
            "sqlite_prompt_id": meta.get("sqlite_prompt_id") or None,
        })
    return out


@mcp.tool()
def similar_decisions(
    query: str,
    k: int = 5,
    max_distance: float = 0.7,
) -> list[dict[str, Any]]:
    """Like search_diagrams but filters out poor matches by cosine distance."""
    raw = search_diagrams(query=query, k=k)
    return [r for r in raw if r["distance"] <= max_distance]


@mcp.tool()
def list_diagrams(limit: int = 20, tag_filter: str | None = None) -> list[dict[str, Any]]:
    """Browse stored diagrams without semantic search."""
    coll = _get_collection()
    where = {"tags": {"$contains": tag_filter}} if tag_filter else None
    res = coll.get(limit=limit, where=where) if where else coll.get(limit=limit)
    out = []
    for did, meta in zip(res.get("ids", []), res.get("metadatas", [])):
        out.append({
            "id": did,
            "title": meta.get("title", ""),
            "tags": (meta.get("tags") or "").split(",") if meta.get("tags") else [],
            "ingested_at": meta.get("ingested_at"),
        })
    return out


@mcp.tool()
def collection_stats() -> dict[str, Any]:
    """Diagnostic: count, embed model, storage path."""
    coll = _get_collection()
    return {
        "collection": _COLLECTION,
        "path": str(_CHROMA_PATH),
        "embed_model": _EMBED_MODEL,
        "count": coll.count(),
    }


@mcp.tool()
def delete_diagram(diagram_id: str) -> dict[str, Any]:
    """Remove a diagram by id."""
    coll = _get_collection()
    coll.delete(ids=[diagram_id])
    log.info("deleted mermaid %s", diagram_id)
    return {"deleted": True, "id": diagram_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    log.info("mermaid-vector MCP server starting; chroma=%s collection=%s",
             _CHROMA_PATH, _COLLECTION)
    mcp.run()
