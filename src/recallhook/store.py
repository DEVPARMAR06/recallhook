"""Guards the memory store against an embedding-model change, and rebuilds it when asked.

Vectors from different embedding models (or sizes) cannot be mixed in one store, so the
store remembers which model built it. Changing `embed_model` without reindexing is refused
with an explanation instead of silently returning garbage."""
from __future__ import annotations

import copy
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config


class StoreMismatch(RuntimeError):
    pass


def meta_path(cfg: Config) -> Path:
    return cfg.home / "store_meta.json"


def read_meta(cfg: Config) -> dict | None:
    try:
        return json.loads(meta_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_meta(cfg: Config) -> None:
    cfg.home.mkdir(parents=True, exist_ok=True)
    meta_path(cfg).write_text(
        json.dumps({"embed_model": cfg.embed_model, "embed_dims": cfg.embed_dims}, indent=2) + "\n",
        encoding="utf-8")


def check(cfg: Config, write: bool = True) -> None:
    """Raise StoreMismatch if the configured embedder differs from the one that built the store."""
    meta = read_meta(cfg)
    if meta is None:
        if write:
            write_meta(cfg)  # first run (or a store from before this guard): trust current config
        return
    if (meta.get("embed_model"), meta.get("embed_dims")) != (cfg.embed_model, cfg.embed_dims):
        raise StoreMismatch(
            f"The memory store was built with embedding model '{meta.get('embed_model')}' "
            f"({meta.get('embed_dims')} dims) but the config now says '{cfg.embed_model}' "
            f"({cfg.embed_dims} dims). Run `recallhook reindex` to rebuild it with the new model "
            f"(your memories are kept), or switch back with "
            f"`recallhook config set embed_model {meta.get('embed_model')}`.")


def reindex(cfg: Config, builder: Callable[..., Any]) -> int:
    """Re-embed every stored memory with the configured embedder. Returns how many were kept.

    The old vectors are moved to `qdrant.bak-<time>` first, so nothing is lost if this fails.
    The service must not be holding the store open (the CLI pauses it)."""
    meta = read_meta(cfg)
    if meta is None or (meta.get("embed_model"), meta.get("embed_dims")) == (cfg.embed_model, cfg.embed_dims):
        return 0  # nothing to do
    old_cfg = copy.copy(cfg)
    old_cfg.embed_model, old_cfg.embed_dims = meta["embed_model"], meta["embed_dims"]
    old = builder(old_cfg, check=False)
    points = old.vector_store.list(None, 100000)
    points = points[0] if isinstance(points, tuple) else points
    items = [(p.payload.get("data"), p.payload.get("user_id")) for p in points if p.payload.get("data")]
    try:
        old.vector_store.client.close()
    except Exception:  # noqa: BLE001 - best effort: releases the local store lock
        pass
    del old
    src = cfg.home / "qdrant"
    if src.exists():
        shutil.move(str(src), str(cfg.home / f"qdrant.bak-{int(time.time())}"))
    new = builder(cfg, check=False)
    for text, user_id in items:
        new.add([{"role": "user", "content": text}], user_id=user_id, infer=False)
    try:
        new.vector_store.client.close()
    except Exception:  # noqa: BLE001
        pass
    write_meta(cfg)
    return len(items)
