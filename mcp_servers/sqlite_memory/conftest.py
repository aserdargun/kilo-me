"""Local conftest: load this directory's server.py under a unique module name."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _fresh_load(env: dict[str, str]) -> object:
    """(Re)load server.py with the given env applied. Each call is fresh."""
    name = "_mcp_sqlite_memory"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _HERE / "server.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    # Apply env before executing module body so module-level reads pick it up
    import os
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
def memory_module(tmp_path: Path):
    db = tmp_path / "memory.sqlite"
    mod = _fresh_load({"MEMORY_DB_PATH": str(db)})
    mod._ensure_schema()
    return mod
