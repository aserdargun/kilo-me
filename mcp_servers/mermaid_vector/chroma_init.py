#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "chromadb>=0.5.0",
#   "sentence-transformers>=2.7.0",
# ]
# ///
"""
chroma_init.py — standalone helper to initialize, inspect, or reset the
ChromaDB collection used by the mermaid-vector MCP server.

Default Chroma path: $XDG_CONFIG_HOME/kilo/chroma (typically ~/.config/kilo/chroma).
Override via CHROMA_PATH env var.

Usage:
    uv run chroma_init.py status
    uv run chroma_init.py init
    uv run chroma_init.py reset --yes
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

_XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
_KILO_HOME = Path(os.environ.get("KILO_HOME") or (_XDG_CONFIG_HOME / "kilo"))
_DEFAULT_CHROMA = _KILO_HOME / "chroma"
_CHROMA_PATH = Path(os.environ.get("CHROMA_PATH", str(_DEFAULT_CHROMA)))
_COLLECTION = os.environ.get("CHROMA_COLLECTION", "decisions")
_EMBED_MODEL = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")


def _client() -> chromadb.api.ClientAPI:
    _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(_CHROMA_PATH))


def cmd_init() -> int:
    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=_EMBED_MODEL)
    coll = _client().get_or_create_collection(
        name=_COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    print(f"OK: collection '{_COLLECTION}' ready at {_CHROMA_PATH} (count={coll.count()})")
    return 0


def cmd_status() -> int:
    if not _CHROMA_PATH.exists():
        print(f"NOT INITIALIZED: {_CHROMA_PATH} does not exist")
        return 1
    try:
        coll = _client().get_collection(name=_COLLECTION)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        return 2
    print(f"path:       {_CHROMA_PATH}")
    print(f"collection: {_COLLECTION}")
    print(f"embed:      {_EMBED_MODEL}")
    print(f"count:      {coll.count()}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if not args.yes:
        print("Refusing to reset without --yes")
        return 1
    if _CHROMA_PATH.exists():
        shutil.rmtree(_CHROMA_PATH)
        print(f"removed {_CHROMA_PATH}")
    return cmd_init()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the kilo-me ChromaDB collection")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the collection if missing")
    sub.add_parser("status", help="show collection stats")
    reset = sub.add_parser("reset", help="DESTRUCTIVE: wipe and recreate")
    reset.add_argument("--yes", action="store_true", help="confirm destruction")
    args = parser.parse_args(argv)
    return {
        "init": lambda: cmd_init(),
        "status": lambda: cmd_status(),
        "reset": lambda: cmd_reset(args),
    }[args.cmd]()


if __name__ == "__main__":
    sys.exit(main())
