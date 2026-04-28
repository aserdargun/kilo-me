"""Tests for the auth.json loader. Run with: pytest mcp_servers/test_auth.py"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent


def _fresh_load(env: dict[str, str | None]) -> object:
    """(Re)load _auth.py with the given env applied. Each call is fresh."""
    name = "_kilo_auth_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, _HERE / "_auth.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
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
def auth_json(tmp_path):
    """Factory: create an auth.json file with given content + permissions."""
    def _make(payload: dict, mode: int = 0o600, name: str = "auth.json") -> Path:
        p = tmp_path / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(p, mode)
        return p
    return _make


@pytest.fixture()
def clean_env(monkeypatch):
    """Strip every env var the loader could touch."""
    for k in (
        "AUTH_FILE", "XDG_DATA_HOME",
        "OPENROUTER_API_KEY", "BP_REPO_PATH", "BP_REMOTE_BRANCH",
        "BP_AUTO_MERGE", "BP_DECISIONS_DIR",
    ):
        monkeypatch.delenv(k, raising=False)


# ─── Path resolution ─────────────────────────────────────────────────────────

def test_explicit_auth_file_wins(auth_json, clean_env, monkeypatch):
    p = auth_json({"openrouter": {"type": "api", "key": "from-explicit"}})
    monkeypatch.setenv("AUTH_FILE", str(p))
    monkeypatch.setenv("XDG_DATA_HOME", "/nowhere/special")
    mod = _fresh_load({})
    assert mod.auth_path() == p
    assert os.environ["OPENROUTER_API_KEY"] == "from-explicit"


def test_default_path_when_unset(clean_env):
    mod = _fresh_load({})
    expected = Path.home() / ".local" / "share" / "kilo" / "auth.json"
    assert mod.auth_path() == expected


def test_xdg_data_home_path(auth_json, clean_env, tmp_path, monkeypatch):
    xdg = tmp_path / "data_home"
    (xdg / "kilo").mkdir(parents=True)
    p = xdg / "kilo" / "auth.json"
    p.write_text(json.dumps({"openrouter": {"type": "api", "key": "from-xdg"}}))
    os.chmod(p, 0o600)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.delenv("AUTH_FILE", raising=False)
    mod = _fresh_load({})  # env already in place via monkeypatch
    assert mod.auth_path() == p
    assert os.environ["OPENROUTER_API_KEY"] == "from-xdg"


# ─── Loading semantics ───────────────────────────────────────────────────────

def test_existing_env_is_never_overridden(auth_json, clean_env, monkeypatch):
    p = auth_json({"openrouter": {"type": "api", "key": "from-file"}})
    monkeypatch.setenv("AUTH_FILE", str(p))
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-shell")
    _fresh_load({"AUTH_FILE": str(p), "OPENROUTER_API_KEY": "from-shell"})
    assert os.environ["OPENROUTER_API_KEY"] == "from-shell"


def test_loads_multiple_keys(auth_json, clean_env, monkeypatch):
    p = auth_json({
        "openrouter": {"type": "api", "key": "sk-or-test"},
        "BP_REPO_PATH": "/path/to/repo",
        "BP_AUTO_MERGE": "1",
    })
    monkeypatch.setenv("AUTH_FILE", str(p))
    _fresh_load({"AUTH_FILE": str(p)})
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-test"
    assert os.environ["BP_REPO_PATH"] == "/path/to/repo"
    assert os.environ["BP_AUTO_MERGE"] == "1"


def test_missing_file_is_silent(clean_env, monkeypatch, tmp_path, capsys):
    p = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("AUTH_FILE", str(p))
    mod = _fresh_load({"AUTH_FILE": str(p)})
    keys = mod.load_auth()
    assert keys == {}
    out = capsys.readouterr()
    # No noisy warning when the file is just absent
    assert "could not read" not in out.err


def test_malformed_json_warns(clean_env, monkeypatch, tmp_path, capsys):
    p = tmp_path / "bad.json"
    p.write_text("not json {{{")
    os.chmod(p, 0o600)
    monkeypatch.setenv("AUTH_FILE", str(p))
    mod = _fresh_load({"AUTH_FILE": str(p)})
    out = capsys.readouterr()
    assert "could not read" in out.err
    # Loader returns empty dict, doesn't raise
    assert mod.load_auth() == {}


def test_non_object_root_warns(clean_env, monkeypatch, tmp_path, capsys):
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["not", "an", "object"]))
    os.chmod(p, 0o600)
    monkeypatch.setenv("AUTH_FILE", str(p))
    mod = _fresh_load({"AUTH_FILE": str(p)})
    out = capsys.readouterr()
    assert "is not a JSON object" in out.err
    assert mod.load_auth() == {}


def test_non_string_values_skipped(auth_json, clean_env, monkeypatch):
    p = auth_json({
        "openrouter": {"type": "api", "key": "valid"},
        "NESTED": {"foo": "bar"},   # skipped — no "key" field
        "NUMERIC": 42,              # skipped
        "FLAG": True,               # skipped
    })
    monkeypatch.setenv("AUTH_FILE", str(p))
    mod = _fresh_load({"AUTH_FILE": str(p)})
    keys = mod.load_auth()
    assert "OPENROUTER_API_KEY" in keys
    assert "NESTED_API_KEY" not in keys
    assert "NUMERIC" not in keys
    assert "FLAG" not in keys


def test_kilo_native_format(auth_json, clean_env, monkeypatch):
    """Provider objects with {type, key} are mapped to <PROVIDER>_API_KEY."""
    p = auth_json({"openrouter": {"type": "api", "key": "sk-or-native"}})
    monkeypatch.setenv("AUTH_FILE", str(p))
    _fresh_load({"AUTH_FILE": str(p)})
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-native"


def test_flat_format_still_works(auth_json, clean_env, monkeypatch):
    """Legacy flat format remains supported alongside the native format."""
    p = auth_json({"OPENROUTER_API_KEY": "sk-or-flat"})
    monkeypatch.setenv("AUTH_FILE", str(p))
    _fresh_load({"AUTH_FILE": str(p)})
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-flat"


# ─── Permission warning ──────────────────────────────────────────────────────

def test_warns_on_loose_permissions(auth_json, clean_env, monkeypatch, capsys):
    p = auth_json({"OPENROUTER_API_KEY": "secret"}, mode=0o644)
    monkeypatch.setenv("AUTH_FILE", str(p))
    _fresh_load({"AUTH_FILE": str(p)})
    out = capsys.readouterr()
    assert "loose permissions" in out.err
    assert "chmod 600" in out.err


def test_no_warning_on_strict_permissions(auth_json, clean_env, monkeypatch, capsys):
    p = auth_json({"OPENROUTER_API_KEY": "secret"}, mode=0o600)
    monkeypatch.setenv("AUTH_FILE", str(p))
    _fresh_load({"AUTH_FILE": str(p)})
    out = capsys.readouterr()
    assert "loose permissions" not in out.err
