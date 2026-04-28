"""Local conftest: load this directory's server.py under a unique module name."""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent


def _fresh_load(env: dict[str, str]) -> object:
    name = "_mcp_mermaid_vector"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _HERE / "server.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        spec.loader.exec_module(mod)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


@pytest.fixture()
def mv_module(tmp_path: Path):
    mod = _fresh_load({
        "CHROMA_PATH": str(tmp_path / "chroma"),
        "CHROMA_COLLECTION": "test_decisions",
    })
    # Reset any lazy-init globals so each test gets a fresh client
    mod._client = None
    mod._collection = None
    return mod
