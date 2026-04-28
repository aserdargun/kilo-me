"""End-to-end smoke test exercising all four MCP servers together.

Uses the `load_mcp_server` fixture from tests/conftest.py to load each server
under a unique module name, sidestepping the duplicate-basename trap.
"""
from __future__ import annotations

import pytest

pytest.importorskip("chromadb", exc_type=ImportError)
pytest.importorskip("sentence_transformers", exc_type=ImportError)
pytest.importorskip("kuzu", exc_type=ImportError)


@pytest.fixture()
def stack(load_mcp_server, tmp_path, monkeypatch):
    """Wire up all four MCP servers against temp paths."""
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.sqlite"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("CHROMA_COLLECTION", "smoke")
    monkeypatch.setenv("MODELS_CACHE_PATH", str(tmp_path / "models.json"))
    monkeypatch.setenv("GRAPH_DB_PATH", str(tmp_path / "graph.kuzu"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    sm = load_mcp_server("sqlite_memory")
    sm._ensure_schema()

    om = load_mcp_server("openrouter_models")

    mv = load_mcp_server("mermaid_vector")
    mv._client = None
    mv._collection = None

    gm = load_mcp_server("graph_memory")
    # Reset the module-level connection so this test gets its own DB handle
    # against the GRAPH_DB_PATH set above.
    gm._db = None
    gm._conn = None

    return {"sm": sm, "om": om, "mv": mv, "gm": gm}


def test_full_task_lifecycle(stack, monkeypatch):
    sm, om, mv, gm = stack["sm"], stack["om"], stack["mv"], stack["gm"]

    # 1. Architect-style: ingest a Mermaid plan
    diagram = """%% title: smoke test plan
%% tags: smoke, e2e
flowchart LR
  start --> middle --> finish
"""
    mv_res = mv.ingest_mermaid(
        diagram=diagram,
        title="Smoke test plan",
        tags=["smoke", "e2e", "success"],
    )
    assert mv_res["ingested"] is True
    mermaid_id = mv_res["id"]

    # 2. Coder-style: log task start
    sm_start = sm.save_prompt(
        agent="code", model="moonshotai/kimi-k2.6",
        prompt="implement smoke test", success=None,
        tags=["smoke", "e2e"],
    )
    assert sm_start["updated"] is False

    # 3. Coder-style: log task end with mermaid id
    sm_end = sm.save_prompt(
        agent="code", model="moonshotai/kimi-k2.6",
        prompt="implement smoke test",
        completion="all green", success=True,
        tokens_in=120, tokens_out=80,
        tags=["smoke", "e2e", "success"],
        mermaid_id=mermaid_id,
    )
    assert sm_end["id"] == sm_start["id"]
    assert sm_end["updated"] is True

    # 4. Search both stores
    sm_hits = sm.search_prompts("smoke")
    assert len(sm_hits) == 1
    assert sm_hits[0]["mermaid_id"] == mermaid_id

    mv_hits = mv.search_diagrams("smoke test plan", k=1)
    assert len(mv_hits) == 1
    assert mv_hits[0]["id"] == mermaid_id

    # 5. Pattern detection — only 1 row, so not promotion-ready
    pattern = sm.count_pattern("smoke", only_successful=True)
    assert pattern["count"] == 1
    assert pattern["promotion_ready"] is False

    # 6. OpenRouter scoring works without network
    fake_models = [
        {
            "id": "moonshotai/kimi-k2.6",
            "context_length": 256_000,
            "supported_parameters": ["tools"],
            "pricing": {"completion": "0.000004"},
        },
    ]
    monkeypatch.setattr(om, "_fetch_models", lambda category=None: fake_models)
    om.refresh_catalog()
    pick = om.model_for_budget(task_kind="code", max_cost_per_mtok=10.0)
    assert pick["id"] == "moonshotai/kimi-k2.6"

    # 7. Memory Curator handoff into the graph: Prompt + Tag + Diagram + edges
    pid = sm_end["id"]
    gm.add_node("Prompt", pid,
                {"agent": "coder-ch", "model": "moonshotai/kimi-k2.6", "success": 1})
    gm.add_node("Tag", "smoke", {"category": "domain"})
    gm.add_node("Tag", "e2e", {"category": "domain"})
    gm.add_node("Diagram", mermaid_id, {"title": "Smoke test plan"})
    gm.add_edge(pid, "smoke", "TAGGED")
    gm.add_edge(pid, "e2e", "TAGGED")
    gm.add_edge(pid, mermaid_id, "DEPICTS")

    nbrs = gm.neighbors(pid, depth=1)
    nbr_ids = {n["id"] for n in nbrs}
    assert {"smoke", "e2e", mermaid_id}.issubset(nbr_ids)

    s = gm.stats()
    assert s["nodes"]["Prompt"] == 1
    assert s["nodes"]["Tag"] == 2
    assert s["nodes"]["Diagram"] == 1
    assert s["edges"]["TAGGED"] == 2
    assert s["edges"]["DEPICTS"] == 1
