"""Tests for sqlite_memory MCP server. Run with: pytest mcp/sqlite_memory/

The `memory_module` fixture lives in conftest.py — it loads this directory's
server.py under a unique module name with a per-test temp DB.
"""
from __future__ import annotations

import datetime as dt


def test_save_prompt_creates_row(memory_module):
    res = memory_module.save_prompt(
        agent="code", model="moonshotai/kimi-k2.6",
        prompt="Implement foo", completion="done", success=True,
        tokens_in=100, tokens_out=200, tags=["mcp", "success"],
    )
    assert "id" in res
    assert len(res["id"]) == 12
    assert res["updated"] is False


def test_save_prompt_is_idempotent_on_same_day(memory_module):
    """Same agent + prompt on the same day = update, not insert."""
    a = memory_module.save_prompt(
        agent="code", model="m1", prompt="task X", success=None,
    )
    b = memory_module.save_prompt(
        agent="code", model="m1", prompt="task X", completion="finished",
        success=True, tokens_in=50, tokens_out=80,
    )
    assert a["id"] == b["id"]
    assert b["updated"] is True


def test_search_prompts_finds_match(memory_module):
    memory_module.save_prompt(
        agent="code", model="m1", prompt="Add chromadb integration",
        completion="OK", success=True, tags=["chromadb", "success"],
    )
    memory_module.save_prompt(
        agent="code", model="m1", prompt="Refactor sqlite schema",
        completion="OK", success=True, tags=["sqlite", "success"],
    )
    results = memory_module.search_prompts("chromadb")
    assert len(results) == 1
    assert "chromadb" in results[0]["prompt"].lower()


def test_search_prompts_filter_by_agent(memory_module):
    memory_module.save_prompt(agent="code", model="m1", prompt="alpha task", success=True)
    memory_module.save_prompt(agent="debug", model="m2", prompt="alpha task", success=True)
    results = memory_module.search_prompts("alpha", agent="debug")
    assert len(results) == 1
    assert results[0]["agent"] == "debug"


def test_count_pattern_promotion_ready(memory_module, monkeypatch):
    """Three rows on three distinct days under the same domain tag should
    flip promotion_ready to True.
    """
    days = [
        dt.datetime(2026, 4, 1, 9, 0, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 2, 9, 0, 0, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 4, 3, 9, 0, 0, tzinfo=dt.timezone.utc),
    ]
    counter = {"i": 0}

    def fake_utcnow() -> dt.datetime:
        d = days[counter["i"] % len(days)]
        counter["i"] += 1
        return d

    monkeypatch.setattr(memory_module, "_utcnow", fake_utcnow)

    for n in range(3):
        memory_module.save_prompt(
            agent="code", model="m1", prompt=f"task {n}",
            completion="ok", success=True, tags=["chromadb", "success"],
        )

    res = memory_module.count_pattern("chromadb")
    assert res["count"] == 3
    assert res["distinct_days"] == 3
    assert res["promotion_ready"] is True


def test_record_promotion(memory_module):
    res = memory_module.record_promotion(
        title="Use chromadb persistent client",
        domain="chromadb",
        prompt_ids=["abc123def456", "abc123def457", "abc123def458"],
        mermaid_ids=["m1", "m2"],
        repo_path="patterns/chromadb-persistent.md",
    )
    assert res["pattern_n"] == 3
