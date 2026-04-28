"""Pytest configuration: a path-based loader for the MCP server modules.

Each MCP server lives in its own directory but the file is named `server.py`,
so importing them by package name causes collisions. This loader uses
importlib's spec-from-file-location API to load each one as a uniquely-named
module, no sys.path manipulation required.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(unique_name: str, file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(unique_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def load_mcp_server():
    """Return a function that loads an MCP server module by directory name.

    Usage:
        def test_x(load_mcp_server, monkeypatch, tmp_path):
            monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "x.sqlite"))
            sm = load_mcp_server("sqlite_memory")
    """
    loaded: list[str] = []

    def _loader(server_dir: str) -> ModuleType:
        unique = f"_mcp_{server_dir}"
        # Always reload so env vars set via monkeypatch take effect.
        sys.modules.pop(unique, None)
        path = _ROOT / "mcp_servers" / server_dir / "server.py"
        mod = _load(unique, path)
        loaded.append(unique)
        return mod

    yield _loader

    for name in loaded:
        sys.modules.pop(name, None)
