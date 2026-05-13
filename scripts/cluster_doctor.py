#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "httpx>=0.27.0",
# ]
# ///
"""
cluster_doctor.py — operator-facing diagnostics for the kilo-me local cluster.

Runs end-to-end: reads auth.json, probes each worker, cross-references the
declared models with what each worker actually has loaded, audits the
fallbacks.json + kilo.jsonc references against reality, and prints a
verdict.

Output is plain text designed to be readable in a 100-column terminal. Exit
code 0 = everything green, 1 = at least one warning, 2 = at least one error.
Useful as a cron probe.

Run from any directory:

    uv run scripts/cluster_doctor.py
    make cluster-doctor                # if Makefile is in scope
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Auth loader (shared)
# ---------------------------------------------------------------------------
def _load_global_auth() -> None:
    here = Path(__file__).resolve().parent
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config / "kilo"))
    candidates = [
        here.parent / "mcp_servers" / "_auth.py",
        kilo_home / "mcp_servers" / "_auth.py",
    ]
    for p in candidates:
        if p.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", p)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_global_auth()


# ANSI styling — no extra deps. Falls back to no-op when not a tty.
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_C = _supports_color()
_GRN = "\033[0;32m" if _C else ""
_YEL = "\033[0;33m" if _C else ""
_RED = "\033[0;31m" if _C else ""
_BOLD = "\033[1m" if _C else ""
_NC = "\033[0m" if _C else ""


def ok(msg: str) -> None:    print(f"  {_GRN}✓{_NC} {msg}")
def warn(msg: str) -> None:  print(f"  {_YEL}!{_NC} {msg}")
def err(msg: str) -> None:   print(f"  {_RED}✗{_NC} {msg}")
def section(title: str) -> None: print(f"\n{_BOLD}{title}{_NC}")


# ---------------------------------------------------------------------------
# Paths and helpers
# ---------------------------------------------------------------------------
def _auth_path() -> Path:
    explicit = os.environ.get("AUTH_FILE")
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "kilo" / "auth.json"


def _kilo_home() -> Path:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return Path(os.environ.get("KILO_HOME") or (xdg / "kilo"))


def _strip_v1(url: str) -> str:
    return url[:-3] if url.endswith("/v1") else url


def _load_jsonc(p: Path) -> dict[str, Any]:
    """Tolerant JSON-with-comments parse for kilo.jsonc."""
    if not p.is_file():
        return {}
    txt = p.read_text(encoding="utf-8")
    txt = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
class Doctor:
    def __init__(self) -> None:
        self.warnings = 0
        self.errors = 0
        self.auth_path = _auth_path()
        self.kilo_home = _kilo_home()
        self.auth: dict[str, Any] = {}
        self.soft_url = ""
        self.hard_url = ""
        self.token = ""
        self.mode = "none"

    def warn(self, msg: str) -> None:
        warn(msg)
        self.warnings += 1

    def err(self, msg: str) -> None:
        err(msg)
        self.errors += 1

    def step_auth(self) -> None:
        section("1. auth.json")
        if not self.auth_path.is_file():
            self.err(f"{self.auth_path} not found — run install.sh first")
            return
        try:
            self.auth = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self.err(f"auth.json is not valid JSON: {e}")
            return
        ok(f"auth.json present at {self.auth_path}")

        soft = self.auth.get("local") or {}
        hard = self.auth.get("local-hard") or {}
        self.soft_url = soft.get("base_url", "").rstrip("/")
        self.hard_url = hard.get("base_url", "").rstrip("/")
        self.token = soft.get("key") or hard.get("key") or ""

        if not self.soft_url and not self.hard_url:
            self.warn("no local cluster configured (local + local-hard both empty)")
            return
        if self.soft_url and self.hard_url and self.soft_url == self.hard_url:
            self.mode = "router"
            ok(f"router mode — both providers → {self.soft_url}")
        else:
            self.mode = "direct"
            ok(f"direct mode — soft: {self.soft_url or '<unset>'}, hard: {self.hard_url or '<unset>'}")
        if not self.token:
            self.warn("no shared token set on either provider — router will reject requests")

    def step_workers(self) -> None:
        section("2. worker reachability")
        if self.mode == "none":
            return

        if self.mode == "router":
            url = _strip_v1(self.soft_url)
            self._probe_router(url)
            return

        for tier, url in (("soft", self.soft_url), ("hard", self.hard_url)):
            if not url:
                continue
            self._probe_worker(tier, _strip_v1(url))

    def _probe_router(self, base: str) -> None:
        try:
            t0 = time.time()
            r = httpx.get(f"{base}/healthz", timeout=5.0)
            elapsed = (time.time() - t0) * 1000
            if r.status_code >= 400:
                self.err(f"router /healthz returned HTTP {r.status_code}")
                return
            data = r.json()
            ok(f"router /healthz reachable in {elapsed:.0f} ms")
            for tier, info in (data.get("workers") or {}).items():
                if info.get("healthy"):
                    ok(f"  {tier} tier @ {info.get('url')} — healthy")
                else:
                    self.warn(f"  {tier} tier @ {info.get('url')} — DOWN ({info.get('last_error')})")
        except httpx.HTTPError as exc:
            self.err(f"router unreachable: {exc}")
        except (ValueError, KeyError) as exc:
            self.err(f"router returned malformed /healthz: {exc}")

    def _probe_worker(self, tier: str, base: str) -> None:
        try:
            t0 = time.time()
            r = httpx.get(f"{base}/api/tags", timeout=5.0)
            elapsed = (time.time() - t0) * 1000
            if r.status_code >= 400:
                self.err(f"{tier} @ {base} returned HTTP {r.status_code}")
                return
            data = r.json()
            model_count = len(data.get("models") or [])
            ok(f"{tier} @ {base} — {model_count} models loaded, {elapsed:.0f} ms")
        except httpx.HTTPError as exc:
            self.err(f"{tier} @ {base} unreachable: {exc}")
        except (ValueError, KeyError) as exc:
            self.err(f"{tier} @ {base} returned malformed /api/tags: {exc}")

    def step_models(self) -> None:
        section("3. model availability vs fallbacks.json")
        if self.mode == "none":
            return
        fb_path = self.kilo_home / "fallbacks.json"
        if not fb_path.is_file():
            self.warn(f"{fb_path} not present — skipping model audit")
            return
        try:
            chains = json.loads(fb_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self.err(f"fallbacks.json malformed: {e}")
            return

        # Collect every local* slug referenced anywhere.
        wanted_soft: set[str] = set()
        wanted_hard: set[str] = set()
        for agent, chain in chains.items():
            if agent.startswith("_") or not isinstance(chain, list):
                continue
            for slug in chain:
                if isinstance(slug, str):
                    if slug.startswith("local-hard/"):
                        wanted_hard.add(slug[len("local-hard/"):])
                    elif slug.startswith("local/"):
                        wanted_soft.add(slug[len("local/"):])

        # Probe each worker for what's actually loaded.
        soft_loaded = self._models_at(_strip_v1(self.soft_url)) if self.soft_url else set()
        hard_loaded = self._models_at(_strip_v1(self.hard_url)) if self.hard_url else set()
        if self.mode == "router":
            # Both URLs hit the router; ask /v1/models for the union.
            soft_loaded, hard_loaded = self._models_via_router()

        missing_soft = sorted(wanted_soft - soft_loaded)
        missing_hard = sorted(wanted_hard - hard_loaded)

        if soft_loaded:
            ok(f"soft worker has {len(soft_loaded)} model(s) loaded")
        if hard_loaded:
            ok(f"hard worker has {len(hard_loaded)} model(s) loaded")
        for m in missing_soft:
            self.warn(f"fallbacks.json references soft model not loaded: {m}")
        for m in missing_hard:
            self.warn(f"fallbacks.json references hard model not loaded: {m}")
        if not missing_soft and not missing_hard:
            ok("every local-tier model referenced in fallbacks.json is loaded")

    def _models_at(self, base: str) -> set[str]:
        try:
            r = httpx.get(f"{base}/api/tags", timeout=5.0)
            r.raise_for_status()
            return {m.get("name") for m in (r.json().get("models") or []) if m.get("name")}
        except (httpx.HTTPError, ValueError, KeyError):
            return set()

    def _models_via_router(self) -> tuple[set[str], set[str]]:
        if not self.soft_url:
            return set(), set()
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = httpx.get(f"{self.soft_url}/models", headers=headers, timeout=5.0)
            r.raise_for_status()
            soft: set[str] = set()
            hard: set[str] = set()
            for m in (r.json().get("data") or []):
                tier = m.get("owned_by", "soft")
                if tier == "hard":
                    hard.add(m.get("id"))
                else:
                    soft.add(m.get("id"))
            return soft, hard
        except (httpx.HTTPError, ValueError, KeyError):
            return set(), set()

    def step_kilo_jsonc(self) -> None:
        section("4. kilo.jsonc agent matrix")
        path = self.kilo_home / "kilo.jsonc"
        cfg = _load_jsonc(path)
        if not cfg:
            self.warn(f"{path} not parseable — skipping matrix audit")
            return
        agents = cfg.get("agent") or {}
        lo_slots = sorted(s for s in agents if s.endswith("-lo"))
        if not lo_slots:
            self.warn("no -lo agents declared in kilo.jsonc")
            return
        ok(f"found {len(lo_slots)} -lo agents in kilo.jsonc")
        for slot in lo_slots:
            model = (agents[slot] or {}).get("model", "")
            if not model.startswith(("local/", "local-hard/")):
                self.warn(f"  {slot}: model {model!r} is not a local/* slug")


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    print(f"{_BOLD}kilo-me cluster doctor{_NC}")

    d = Doctor()
    d.step_auth()
    d.step_workers()
    d.step_models()
    d.step_kilo_jsonc()

    section("verdict")
    if d.errors:
        err(f"{d.errors} error(s), {d.warnings} warning(s)")
        return 2
    if d.warnings:
        warn(f"0 errors, {d.warnings} warning(s)")
        return 1
    ok("everything green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
