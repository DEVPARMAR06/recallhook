"""mem0 on local Ollama: fail-open search, background (queued) writes."""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import time
import threading
from typing import Any, Callable

from .config import Config
from .events import EventLog
from .inject import text_key
from . import diagnostics, store

log = logging.getLogger("recallhook")


def build_mem0(cfg: Config, check: bool = True) -> Any:
    if check:
        store.check(cfg)  # refuses an embedding-model change that would corrupt recall
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    from mem0 import Memory  # imported late: slow, and optional for tests

    store_dir = os.path.expanduser(cfg.store_path)
    os.makedirs(store_dir, exist_ok=True)
    m = Memory.from_config({
        "llm": {"provider": "ollama", "config": {
            "model": cfg.llm_model, "temperature": 0, "max_tokens": 800,
            "ollama_base_url": cfg.ollama_url}},
        "embedder": {"provider": "ollama", "config": {
            "model": cfg.embed_model, "embedding_dims": cfg.embed_dims,
            "ollama_base_url": cfg.ollama_url}},
        "vector_store": {"provider": "qdrant", "config": {
            "collection_name": "recallhook", "path": os.path.join(store_dir, "qdrant"),
            "embedding_model_dims": cfg.embed_dims, "on_disk": True}},
        "history_db_path": os.path.join(store_dir, "history.db"),
    })
    tune_ollama(m, cfg)
    return m


def tune_ollama(m: Any, cfg: Config) -> None:
    """Pin both models in memory (keep_alive), cap the LLM context (a 32k default
    spills 7B models onto the CPU) and turn off 'thinking' where the model has it."""
    llm_chat = m.llm.client.chat
    state = {"think": True}

    def chat(**kw):
        kw["options"] = {**kw.get("options", {}), "num_ctx": cfg.num_ctx}
        kw.setdefault("keep_alive", cfg.keep_alive)
        if state["think"]:
            kw["think"] = False
        try:
            return llm_chat(**kw)
        except Exception as e:  # noqa: BLE001 - model has no thinking mode
            if state["think"] and "think" in str(e).lower():
                state["think"] = False
                kw.pop("think", None)
                return llm_chat(**kw)
            raise

    m.llm.client.chat = chat
    embed = m.embedding_model.client.embed
    # Embeddings on the CPU: while the LLM generates on the GPU, Ollama otherwise
    # makes the tiny embedding model wait (measured: multi-second stalls on recall).
    opts = {"num_gpu": 0} if cfg.embed_on_cpu else {}
    m.embedding_model.client.embed = lambda **kw: embed(
        **{"keep_alive": cfg.keep_alive, "options": opts, **kw})


EXTRACT_RULES = (
    "Only extract durable facts worth remembering in future sessions: the user's "
    "preferences, tools and conventions, project names and setup, infrastructure details, "
    "and decisions. Do NOT extract one-off tasks, requests, questions, bug reports or "
    "anything about what the user wants done right now. If the message has no durable "
    "fact, return no facts."
)


class MemoryService:
    """Search is embedding-only (fast). Writes use the LLM (slow) so they are
    queued and run on one background thread, never on the request path."""

    def __init__(self, cfg: Config, factory: Callable[[Config], Any] = build_mem0):
        self.cfg = cfg
        self._factory = factory
        self._client: Any = None
        self._init_lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=cfg.queue_size)
        self._worker: threading.Thread | None = None
        self.stats = {"searches": 0, "search_failures": 0, "writes": 0, "write_failures": 0, "dropped": 0}
        self.events = EventLog(cfg.home / "events.db")
        self._captured: set[str] = set()
        self.init_error: str | None = None
        self._diag: tuple[float, list[str]] = (0.0, [])

    def client(self) -> Any:
        with self._init_lock:
            if self._client is None:
                try:
                    self._client = self._factory(self.cfg)
                    self.init_error = None
                except Exception as e:  # noqa: BLE001 - remember why, for the dashboard/status
                    self.init_error = str(e) if isinstance(e, store.StoreMismatch) else (
                        f"Could not start the memory engine ({type(e).__name__}). "
                        f"Run `recallhook status` for details.")
                    raise
            return self._client

    def problems(self) -> list[str]:
        """Plain-language reasons memory is not working right now (cached for 30 s)."""
        at, cached = self._diag
        if time.time() - at > 30:
            cached = diagnostics.check(self.cfg)
            self._diag = (time.time(), cached)
        out = list(cached)
        if self.init_error and self.init_error not in out:
            out.append(self.init_error)
        return out

    def warm(self) -> None:
        """Build the client and load the embedding model so the first real
        search does not blow the timeout on a cold Ollama model load."""
        self.search_sync("warmup", self.cfg.user_id)
        # Also load the extraction LLM now: a model load mid-session blocks Ollama's
        # scheduler and would stall recalls.
        self.client().llm.generate_response([{"role": "user", "content": "Reply with: ok"}])

    def start(self) -> None:
        if self._worker is None and self.cfg.write:
            self._worker = threading.Thread(target=self._run, name="mem0-writer", daemon=True)
            self._worker.start()

    # -- search ------------------------------------------------------------
    def search_sync(self, query: str, user_id: str) -> list[str]:
        res = self.client().search(
            query, filters={"user_id": user_id}, top_k=self.cfg.top_k, threshold=self.cfg.min_score)
        items = res.get("results", []) if isinstance(res, dict) else res
        return [r["memory"] for r in items if r.get("memory")]

    async def search(self, query: str, user_id: str, source: str = "proxy") -> list[str] | None:
        """Never raises; returns None on timeout or error (fail-open)."""
        items = await self.recall(query, user_id, source)
        return None if items is None else [i["memory"] for i in items]

    async def recall(self, query: str, user_id: str, source: str = "hook") -> list[dict] | None:
        """Like search but keeps ids/scores and logs a dashboard event."""
        self.stats["searches"] += 1
        t0 = time.time()
        try:
            items = await asyncio.wait_for(
                asyncio.to_thread(self._search_items, query, user_id), self.cfg.search_timeout)
        except Exception as e:  # noqa: BLE001 - fail open on anything
            self.stats["search_failures"] += 1
            self.events.add("recall_failed", source, ms=int((time.time() - t0) * 1000), detail=type(e).__name__)
            log.warning("memory search skipped: %s", type(e).__name__)
            return None
        ms = int((time.time() - t0) * 1000)
        chars = sum(len(i["memory"]) + 4 for i in items)
        self.events.add("recall", source, hits=len(items), ms=ms, tokens=chars // 4,
                        detail=" | ".join(i["memory"][:70] for i in items[:3]))
        return items

    def _search_items(self, query: str, user_id: str) -> list[dict]:
        res = self.client().search(
            query, filters={"user_id": user_id}, top_k=self.cfg.top_k, threshold=self.cfg.min_score)
        items = res.get("results", []) if isinstance(res, dict) else res
        return [{"id": r["id"], "memory": r["memory"], "score": round(r.get("score", 0), 3)}
                for r in items if r.get("memory")]

    def capture(self, text: str, user_id: str) -> str | None:
        """Queue text for extraction once. Returns None when queued, otherwise the reason it was not."""
        if not self.cfg.write:
            return "capture is turned off"
        if len(text) < self.cfg.min_chars:
            return "too short or a command"
        key = text_key(text)
        if key in self._captured:
            return "already stored"
        self._captured.add(key)
        return None if self.submit(text, user_id) else "queue full"

    # -- write -------------------------------------------------------------
    def submit(self, text: str, user_id: str) -> bool:
        if not self.cfg.write:
            return False
        try:
            self._q.put_nowait((text, user_id))
            return True
        except queue.Full:
            self.stats["dropped"] += 1
            return False

    def _run(self) -> None:
        while True:
            text, user_id = self._q.get()
            t0 = time.time()
            try:
                res = self._add(text, user_id)
                self.stats["writes"] += 1
                n = len(res.get("results", [])) if isinstance(res, dict) else 0
                self.stats["memories_stored"] = self.stats.get("memories_stored", 0) + n
                log.info("captured %d chars -> %d memories", len(text), n)
                made = [r.get("memory", "") for r in res.get("results", [])] if isinstance(res, dict) else []
                self.events.add("stored", "capture", hits=n, ms=int((time.time() - t0) * 1000),
                                detail=" | ".join(m[:70] for m in made[:3]))
                if os.environ.get("RECALLHOOK_DEBUG"):
                    log.info("captured text: %r -> %s", text[:300], res)
            except Exception as e:  # noqa: BLE001
                self.stats["write_failures"] += 1
                log.warning("memory write failed: %s", type(e).__name__)
            finally:
                self._q.task_done()

    def _add(self, text: str, user_id: str) -> Any:
        """mem0 swallows extraction errors and returns no results. Chit-chat
        legitimately yields none too, so retry exactly once before giving up."""
        msgs = [{"role": "user", "content": text}]
        res = self.client().add(msgs, user_id=user_id, prompt=EXTRACT_RULES)
        if isinstance(res, dict) and not res.get("results"):
            res = self.client().add(msgs, user_id=user_id, prompt=EXTRACT_RULES)
            self.stats["retries"] = self.stats.get("retries", 0) + 1
        return res

    # -- inspection (used by the web UI) ------------------------------------
    def list_all(self, user_id: str) -> list[dict]:
        res = self.client().get_all(filters={"user_id": user_id})
        items = res.get("results", []) if isinstance(res, dict) else res
        return [{"id": r["id"], "memory": r["memory"], "created_at": r.get("created_at"),
                 "updated_at": r.get("updated_at")} for r in items]

    def search_scored(self, query: str, user_id: str) -> list[dict]:
        res = self.client().search(query, filters={"user_id": user_id}, top_k=self.cfg.top_k,
                                   threshold=0.0)
        items = res.get("results", []) if isinstance(res, dict) else res
        return [{"id": r["id"], "memory": r["memory"], "score": round(r.get("score", 0), 3)}
                for r in items]

    def delete(self, memory_id: str) -> None:
        self.client().delete(memory_id=memory_id)

    def drain(self) -> None:
        self._q.join()
