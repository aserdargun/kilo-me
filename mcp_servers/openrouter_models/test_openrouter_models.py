"""Tests for openrouter_models MCP server. Run with: pytest mcp/openrouter_models/

The `or_module` fixture lives in conftest.py.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _fake_models(category: str | None = None) -> list[dict]:
    """Offline fake. Accepts the category arg for API compatibility but ignores it."""
    return [
        {
            "id": "moonshotai/kimi-k2.6",
            "context_length": 256_000,
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"completion": "0.000004"},  # $4 per Mtok
        },
        {
            "id": "deepseek/deepseek-v4-pro",
            "context_length": 200_000,
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"completion": "0.0000035"},
        },
        {
            "id": "deepseek/deepseek-v4-flash",
            "context_length": 128_000,
            "supported_parameters": ["tools"],
            "pricing": {"completion": "0.0000003"},
        },
        {
            "id": "qwen/qwen3.6-plus",
            "context_length": 1_000_000,
            "supported_parameters": ["tools"],
            "pricing": {"completion": "0.0000012"},
        },
        {
            "id": "anthropic/claude-opus-4-7",
            "context_length": 200_000,
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"completion": "0.000075"},
        },
        {
            "id": "openai/gpt-5",
            "context_length": 128_000,
            "supported_parameters": ["tools"],
            "pricing": {"completion": "0.00006"},
        },
    ]


@pytest.fixture()
def patched(or_module, monkeypatch):
    """or_module with _fetch_models replaced by an offline fake."""
    monkeypatch.setattr(or_module, "_fetch_models", _fake_models)
    return or_module


def test_score_chinese_beats_western_at_similar_capability(patched):
    chinese = next(m for m in _fake_models() if m["id"].startswith("moonshotai"))
    western = next(m for m in _fake_models() if m["id"].startswith("anthropic"))
    assert patched._score(chinese) > patched._score(western)


def test_refresh_catalog_writes_cache(patched):
    res = patched.refresh_catalog(top_n=10)
    assert res["refreshed"] is True
    assert res["count"] >= 4
    cache = json.loads(Path(patched._CACHE_PATH).read_text())
    assert cache["models"][0]["_score"] >= cache["models"][-1]["_score"]


def test_top_coding_models_returns_n(patched):
    patched.refresh_catalog()
    top3 = patched.top_coding_models(n=3)
    assert len(top3) == 3
    assert top3[0]["is_chinese"] is True


def test_top_coding_models_chinese_only_filter(patched):
    patched.refresh_catalog()
    only_zh = patched.top_coding_models(n=10, chinese_only=True)
    assert all(m["is_chinese"] for m in only_zh)


def test_model_for_budget_respects_cap(patched):
    patched.refresh_catalog()
    pick = patched.model_for_budget(task_kind="code", max_cost_per_mtok=2.0)
    assert pick["id"] in {"deepseek/deepseek-v4-flash", "qwen/qwen3.6-plus"}
    assert pick["completion_per_mtok_usd"] <= 2.0


def test_model_for_budget_chinese_required(patched):
    patched.refresh_catalog()
    pick = patched.model_for_budget(
        task_kind="long_context", max_cost_per_mtok=5.0, require_chinese=True,
    )
    assert pick["id"].split("/")[0] in patched._CHINESE_AUTHORS


def test_cache_status_freshness(patched):
    patched.refresh_catalog()
    status = patched.cache_status()
    assert status["present"] is True
    assert status["fresh"] is True
    assert status["model_count"] >= 4


# ---------------------------------------------------------------------------
# top_free_models — ask-agent routing
# ---------------------------------------------------------------------------
def _fake_models_with_free(category: str | None = None) -> list[dict]:
    """Adds free models alongside the paid ones the other tests use."""
    return _fake_models() + [
        {
            "id": "deepseek/deepseek-v3.2:free",
            "context_length": 128_000,
            "supported_parameters": ["tools"],
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "qwen/qwen3.6-mini:free",
            "context_length": 64_000,
            "supported_parameters": ["tools", "reasoning"],
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "openai/gpt-oss-tiny:free",
            "context_length": 32_000,
            # No tool-calling support
            "supported_parameters": ["reasoning"],
            "pricing": {"prompt": "0", "completion": "0"},
        },
    ]


@pytest.fixture()
def patched_with_free(or_module, monkeypatch):
    monkeypatch.setattr(or_module, "_fetch_models", _fake_models_with_free)
    return or_module


def test_top_free_models_returns_only_free_with_tools(patched_with_free):
    patched_with_free.refresh_catalog()
    free = patched_with_free.top_free_models(limit=10)
    assert len(free) >= 2
    ids = {m["id"] for m in free}
    # Both free + tool-calling models survive
    assert "deepseek/deepseek-v3.2:free" in ids
    assert "qwen/qwen3.6-mini:free" in ids
    # The paid models are excluded
    assert all(m["supports_tools"] for m in free)
    assert "openai/gpt-5" not in ids
    # The free-but-no-tools model is excluded by default
    assert "openai/gpt-oss-tiny:free" not in ids


def test_top_free_models_can_include_no_tool_models(patched_with_free):
    patched_with_free.refresh_catalog()
    free = patched_with_free.top_free_models(limit=10, require_tool_calls=False)
    ids = {m["id"] for m in free}
    assert "openai/gpt-oss-tiny:free" in ids


def test_top_free_models_empty_when_no_free_in_catalog(patched):
    """If the cache contains zero free models, the tool returns []."""
    patched.refresh_catalog()
    assert patched.top_free_models(limit=5) == []


def test_top_free_models_respects_limit(patched_with_free):
    patched_with_free.refresh_catalog()
    one = patched_with_free.top_free_models(limit=1)
    assert len(one) == 1
    # The :free suffix shortcut counts even when prompt/completion missing
    assert one[0]["id"].endswith(":free")


def test_is_free_helper_recognizes_zero_pricing(patched_with_free):
    free_model = {
        "id": "some/model",
        "pricing": {"prompt": "0", "completion": "0"},
    }
    paid_model = {
        "id": "some/other",
        "pricing": {"prompt": "0", "completion": "0.000001"},
    }
    suffix_model = {"id": "vendor/foo:free", "pricing": {}}
    assert patched_with_free._is_free(free_model) is True
    assert patched_with_free._is_free(paid_model) is False
    assert patched_with_free._is_free(suffix_model) is True
