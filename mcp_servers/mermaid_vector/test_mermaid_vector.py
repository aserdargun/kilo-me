"""Tests for mermaid_vector MCP server. Run with: pytest mcp/mermaid_vector/

Skip cleanly if chromadb / sentence-transformers aren't installed, so the
suite stays useful during partial-install development.
"""
from __future__ import annotations

import pytest

pytest.importorskip("chromadb", exc_type=ImportError)
pytest.importorskip("sentence_transformers", exc_type=ImportError)


SAMPLE = """%% title: openrouter refresh flow
%% tags: openrouter, mcp, scheduler
flowchart LR
  cron --> refresh
  refresh --> api
  api --> rank
  rank --> cache
"""

SAMPLE_2 = """flowchart TD
  user --> agent
  agent --> sqlite[(SQLite memory)]
  agent --> chroma[(ChromaDB)]
  sqlite --> curator
  chroma --> curator
"""

INVALID = "this is not mermaid syntax at all"


def test_validate_accepts_valid_diagram(mv_module):
    ok, _ = mv_module._validate_mermaid(SAMPLE)
    assert ok


def test_validate_rejects_garbage(mv_module):
    ok, reason = mv_module._validate_mermaid(INVALID)
    assert not ok
    assert "unrecognized" in reason


def test_ingest_then_search_finds_diagram(mv_module):
    res = mv_module.ingest_mermaid(
        diagram=SAMPLE,
        title="OpenRouter refresh flow",
        tags=["openrouter", "mcp", "scheduler"],
    )
    assert res["ingested"] is True
    assert len(res["id"]) == 12

    found = mv_module.search_diagrams(query="openrouter cron refresh", k=3)
    assert len(found) >= 1
    assert any("openrouter" in (r["title"] or "").lower() for r in found)


def test_ingest_is_idempotent_on_content(mv_module):
    a = mv_module.ingest_mermaid(diagram=SAMPLE, title="A", tags=["x"])
    b = mv_module.ingest_mermaid(diagram=SAMPLE, title="A renamed", tags=["x", "y"])
    assert a["id"] == b["id"]


def test_similar_decisions_filters_by_distance(mv_module):
    mv_module.ingest_mermaid(diagram=SAMPLE, title="OpenRouter", tags=["or"])
    mv_module.ingest_mermaid(diagram=SAMPLE_2, title="Memory pipeline", tags=["mem"])
    strict = mv_module.similar_decisions(query="kubernetes deployment", max_distance=0.1, k=5)
    assert len(strict) <= 1
    loose = mv_module.similar_decisions(query="data pipeline", max_distance=2.0, k=5)
    assert len(loose) >= 1


def test_collection_stats(mv_module):
    mv_module.ingest_mermaid(diagram=SAMPLE, title="X", tags=[])
    stats = mv_module.collection_stats()
    assert stats["count"] >= 1
    assert stats["embed_model"]


def test_delete_diagram(mv_module):
    res = mv_module.ingest_mermaid(diagram=SAMPLE, title="X", tags=[])
    did = res["id"]
    out = mv_module.delete_diagram(did)
    assert out["deleted"]
    found = mv_module.search_diagrams(query="cron refresh openrouter", k=5)
    assert all(r["id"] != did for r in found)
