"""Tests for graph_memory MCP server.

Exercises the Kuzu-backed graph: schema bootstrap, idempotent node/edge
writes, neighbor lookup, and the path-finding helper. The `graph_module`
fixture lives in conftest.py and pins GRAPH_DB_PATH at a tmp_path location.
"""
from __future__ import annotations

import pytest

pytest.importorskip("kuzu", reason="kuzu not installed in this env")


def test_schema_bootstrap_idempotent(graph_module):
    """Calling _connection twice must not raise — schema DDL uses IF NOT EXISTS."""
    graph_module._connection()
    graph_module._connection()
    s = graph_module.stats()
    assert "Prompt" in s["nodes"]
    assert s["nodes"]["Prompt"] == 0


def test_add_node_happy_path(graph_module):
    res = graph_module.add_node(
        "Prompt", "abc123",
        {"agent": "coder-ch", "model": "moonshotai/kimi-k2.6", "success": 1},
    )
    assert res["created"] is True
    assert res["id"] == "abc123"

    s = graph_module.stats()
    assert s["nodes"]["Prompt"] == 1


def test_add_node_idempotent(graph_module):
    """Second add_node with same id is an upsert, not a duplicate."""
    graph_module.add_node("Tag", "mcp", {"category": "domain"})
    graph_module.add_node("Tag", "mcp", {"category": "domain"})
    assert graph_module.stats()["nodes"]["Tag"] == 1


def test_add_node_rejects_unknown_label(graph_module):
    res = graph_module.add_node("Banana", "id1", {})
    assert res["created"] is False
    assert "unknown label" in res["reason"]


def test_add_node_rejects_empty_id(graph_module):
    res = graph_module.add_node("Prompt", "", {})
    assert res["created"] is False
    assert "id is required" in res["reason"]


def test_add_edge_happy_path(graph_module):
    graph_module.add_node("Prompt", "p1", {"agent": "coder-ch", "success": 1})
    graph_module.add_node("Tag", "mcp", {"category": "domain"})
    res = graph_module.add_edge("p1", "mcp", "TAGGED", {"weight": 1})
    assert res["created"] is True
    assert graph_module.stats()["edges"]["TAGGED"] == 1


def test_add_edge_rejects_unknown_rel(graph_module):
    res = graph_module.add_edge("p1", "t1", "FRIENDS_WITH", {})
    assert res["created"] is False
    assert "unknown rel" in res["reason"]


def test_neighbors_finds_direct_links(graph_module):
    graph_module.add_node("Prompt", "p1", {"agent": "coder-ch", "success": 1})
    graph_module.add_node("Tag", "mcp", {"category": "domain"})
    graph_module.add_node("Tag", "tests", {"category": "domain"})
    graph_module.add_edge("p1", "mcp", "TAGGED")
    graph_module.add_edge("p1", "tests", "TAGGED")

    out = graph_module.neighbors("p1", depth=1)
    ids = {n["id"] for n in out}
    assert ids == {"mcp", "tests"}
    assert all(n["label"] == "Tag" for n in out)


def test_neighbors_clamps_depth(graph_module):
    """Depth > 4 silently clamps to 4 (per the docstring)."""
    graph_module.add_node("Prompt", "p1", {"agent": "x", "success": 1})
    graph_module.add_node("Tag", "t1", {})
    graph_module.add_edge("p1", "t1", "TAGGED")
    # depth=99 must not raise — it's clamped before issuing the query
    out = graph_module.neighbors("p1", depth=99)
    assert any(n["id"] == "t1" for n in out)


def test_find_path_returns_endpoints(graph_module):
    graph_module.add_node("Prompt", "p1", {"agent": "coder-ch", "success": 1})
    graph_module.add_node("Decision", "d1", {"pattern_n": 3})
    graph_module.add_edge("p1", "d1", "PROMOTED_TO")

    path = graph_module.find_path("p1", "d1", max_hops=2)
    assert len(path) >= 2
    assert path[0]["id"] == "p1"
    assert path[-1]["id"] == "d1"


def test_find_path_returns_empty_when_unreachable(graph_module):
    graph_module.add_node("Prompt", "p1", {"agent": "x", "success": 1})
    graph_module.add_node("Pattern", "pat1", {"domain": "mcp"})
    # No edge between them
    assert graph_module.find_path("p1", "pat1", max_hops=4) == []


def test_cypher_passthrough(graph_module):
    graph_module.add_node("Tag", "mcp", {"category": "domain"})
    rows = graph_module.cypher("MATCH (t:Tag) RETURN t.id")
    assert ["mcp"] in rows


def test_stats_returns_per_label_counts(graph_module):
    graph_module.add_node("Prompt", "p1", {"agent": "coder-ch", "success": 1})
    graph_module.add_node("Prompt", "p2", {"agent": "coder-ch", "success": 0})
    graph_module.add_node("Tag", "mcp", {})
    graph_module.add_edge("p1", "mcp", "TAGGED")

    s = graph_module.stats()
    assert s["nodes"]["Prompt"] == 2
    assert s["nodes"]["Tag"] == 1
    assert s["edges"]["TAGGED"] == 1
