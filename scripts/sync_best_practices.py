#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
sync_best_practices.py — push promoted patterns to the GitHub repo.

Reads ~/.config/kilo/decisions/*.md (written by the Memory Curator agent) — or
$BP_DECISIONS_DIR if set — and copies them into a sibling clone of the
kilo-best-practices GitHub repo, then commits and pushes.

By default opens a PR via `gh`; set BP_AUTO_MERGE=1 to push straight to main.

Required env:
    BP_REPO_PATH       absolute path to a local clone of kilo-best-practices
    BP_REMOTE_BRANCH   branch to push to (default: main)
    BP_AUTO_MERGE      "1" to skip PR and push directly (default: 0)
    BP_DECISIONS_DIR   override the source dir (default: ~/.config/kilo/decisions
                       falling back to ~/.kilo/decisions or ./docs/decisions)

Credentials: if any of BP_REPO_PATH, BP_REMOTE_BRANCH, BP_AUTO_MERGE,
BP_DECISIONS_DIR are stored in $XDG_DATA_HOME/kilo/auth.json (typically
~/.local/share/kilo/auth.json), they're loaded automatically.

Usage:
    uv run scripts/sync_best_practices.py
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _load_auth_json() -> None:
    """Load shared auth.json loader from a sibling location (best-effort)."""
    here = Path(__file__).resolve().parent
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config_home / "kilo"))
    candidates = [
        here.parent / "mcp_servers" / "_auth.py",
        kilo_home / "mcp_servers" / "_auth.py",
        Path.home() / ".kilo" / "mcp_servers" / "_auth.py",  # legacy
    ]
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("_kilo_auth", path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            return


_load_auth_json()


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] sync_bp: %(message)s",
)
log = logging.getLogger("sync_bp")

TARGET_SUBDIR = "patterns"  # inside the best-practices repo


def _decisions_dir() -> Path:
    """Pick the source directory using this priority:

    1. $BP_DECISIONS_DIR if explicitly set
    2. $KILO_HOME/decisions (typically ~/.config/kilo/decisions)
    3. Legacy ~/.kilo/decisions (for pre-XDG installs)
    4. ./docs/decisions in repo-local mode
    """
    override = os.environ.get("BP_DECISIONS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg_config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    kilo_home = Path(os.environ.get("KILO_HOME") or (xdg_config_home / "kilo"))
    for candidate in (kilo_home / "decisions", Path.home() / ".kilo" / "decisions"):
        if candidate.exists():
            return candidate
    return Path("docs/decisions").resolve()


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    log.debug("$ %s (cwd=%s)", " ".join(cmd), cwd)
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if res.stdout:
        log.debug(res.stdout.strip())
    if res.stderr:
        log.debug(res.stderr.strip())
    if check and res.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{res.stderr}")
    return res


def _ensure_repo(repo: Path) -> None:
    if not repo.exists():
        raise FileNotFoundError(f"BP_REPO_PATH does not exist: {repo}")
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a git repo")


def _copy_decisions(repo: Path, source: Path) -> list[Path]:
    """Copy <source>/*.md into <repo>/patterns/ if newer."""
    target = repo / TARGET_SUBDIR
    target.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        log.warning("no decisions to sync (%s missing)", source)
        return []

    copied: list[Path] = []
    for src in sorted(source.glob("*.md")):
        dst = target / src.name
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            log.debug("skip (up to date): %s", src.name)
            continue
        shutil.copy2(src, dst)
        copied.append(dst)
        log.info("copied %s -> %s", src.name, dst.relative_to(repo))
    return copied


def _has_uncommitted_changes(repo: Path) -> bool:
    res = _run(["git", "status", "--porcelain"], cwd=repo)
    return bool(res.stdout.strip())


def _branch_name() -> str:
    return f"promote/{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d-%H%M%S')}"


def main() -> int:
    repo_str = os.environ.get("BP_REPO_PATH")
    if not repo_str:
        log.error("BP_REPO_PATH is not set")
        return 2
    repo = Path(repo_str).expanduser().resolve()
    _ensure_repo(repo)

    source = _decisions_dir()
    log.info("decisions source: %s", source)

    auto_merge = os.environ.get("BP_AUTO_MERGE", "0") == "1"
    base_branch = os.environ.get("BP_REMOTE_BRANCH", "main")

    _run(["git", "fetch", "origin", base_branch], cwd=repo)
    _run(["git", "checkout", base_branch], cwd=repo)
    _run(["git", "pull", "--ff-only", "origin", base_branch], cwd=repo)

    copied = _copy_decisions(repo, source)
    if not copied:
        log.info("nothing to promote")
        return 0

    if not _has_uncommitted_changes(repo):
        log.info("no diff after copy (target was already up to date)")
        return 0

    branch = base_branch if auto_merge else _branch_name()
    if not auto_merge:
        _run(["git", "checkout", "-b", branch], cwd=repo)

    _run(["git", "add", "."], cwd=repo)
    msg_lines = ["promote: kilo-me patterns", "", "Promoted files:"]
    msg_lines.extend(f"  - {p.relative_to(repo)}" for p in copied)
    msg = "\n".join(msg_lines)
    _run(["git", "commit", "-m", msg], cwd=repo)

    _run(["git", "push", "-u", "origin", branch], cwd=repo)

    if auto_merge:
        log.info("pushed directly to %s (BP_AUTO_MERGE=1)", base_branch)
        return 0

    pr_res = _run(
        ["gh", "pr", "create", "--fill", "--base", base_branch, "--head", branch],
        cwd=repo,
        check=False,
    )
    if pr_res.returncode == 0:
        log.info("PR opened: %s", pr_res.stdout.strip())
    else:
        log.warning("gh CLI not available; open a PR manually for branch %s", branch)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        log.error("sync failed: %s", exc)
        sys.exit(1)
