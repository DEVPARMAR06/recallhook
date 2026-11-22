"""HTTP gateway: client -> (this: mem0 recall + capture) -> Headroom proxy -> provider.

Everything that is not a chat request is streamed through untouched, and any
memory failure falls back to forwarding the request unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import OrderedDict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .config import Config
from .inject import (append_block, format_block, is_memory_path, is_side_request,
                     text_key, user_turns)
from .memory import MemoryService

log = logging.getLogger("recallhook")

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "transfer-encoding", "upgrade"}
USER_HEADER = "x-recallhook-user"
CACHE_MAX = 512


class Gateway:
    def __init__(self, cfg: Config, memory: MemoryService):
        self.cfg, self.memory = cfg, memory
        # text-hash -> injected memories. Re-injecting the *same* block into the
        # same user turn on every later request keeps the conversation prefix
        # byte-identical, so provider prompt caching (and Headroom's cache mode)
        # keeps working.
        self.fallbacks = 0
        self._recalled: OrderedDict[str, list[str]] = OrderedDict()
        self._written: set[str] = set()

    def _remember(self, key: str, memories: list[str]) -> None:
        self._recalled[key] = memories
        self._recalled.move_to_end(key)
        while len(self._recalled) > CACHE_MAX:
            self._recalled.popitem(last=False)

    async def transform(self, body: dict, user_id: str) -> str | None:
        """Inject recalled memories in place. Returns text to capture once the
        upstream call succeeds (see `commit`), or None."""
        if is_side_request(body):
            return None
        turns = user_turns(body)
        if not turns:
            return None
        msgs = body["messages"]
        newest = turns[-1][0]
        for idx, text in turns:
            key = text_key(text)
            if key not in self._recalled and idx == newest and self.cfg.inject:
                found = await self.memory.search(text[:1000], user_id)
                if found is not None:  # None = lookup failed; do not cache
                    self._remember(key, found)
            if self._recalled.get(key):
                append_block(msgs[idx], format_block(self._recalled[key]))
        newest_text = turns[-1][1]
        if (self.cfg.write and text_key(newest_text) not in self._written
                and len(newest_text) >= self.cfg.min_chars):
            return newest_text
        return None

    def commit(self, text: str, user_id: str) -> None:
        """Queue a capture. Called only after a 2xx upstream response, so failed
        or rejected requests never leave memories behind."""
        key = text_key(text)
        if key not in self._written:
            self._written.add(key)
            self.memory.submit(text, user_id)


def create_app(cfg: Config, memory: MemoryService | None = None,
               transport: httpx.AsyncBaseTransport | None = None,
               fallback_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    memory = memory or MemoryService(cfg)
    gw = Gateway(cfg, memory)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        memory.start()
        app.state.http = httpx.AsyncClient(
            base_url=cfg.upstream.rstrip("/"), transport=transport,
            timeout=httpx.Timeout(None, connect=10.0), follow_redirects=False)
        app.state.fallback = (httpx.AsyncClient(
            base_url=cfg.fallback.rstrip("/"), transport=fallback_transport, timeout=httpx.Timeout(None, connect=10.0))
            if cfg.fallback else None)
        warm = asyncio.create_task(asyncio.to_thread(_safe_warm, memory))
        try:
            yield
        finally:
            warm.cancel()
            await app.state.http.aclose()
            if app.state.fallback:
                await app.state.fallback.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.gateway = gw
    from .api import build_api_router
    from .ui import build_router
    app.include_router(build_router(cfg, memory))
    app.include_router(build_api_router(cfg, memory))

    @app.get("/_recallhook/health")
    async def health():
        return {"ok": True, "upstream": cfg.upstream, "llm": cfg.llm_model,
                "embedder": cfg.embed_model, "stats": memory.stats,
                "inject": cfg.inject, "write": cfg.write, "fallbacks": gw.fallbacks}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def forward(request: Request, path: str):
        raw = await request.body()
        user_id = request.headers.get(USER_HEADER) or cfg.user_id
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP | {"host", "content-length", USER_HEADER}}
        capture: str | None = None
        if request.method == "POST" and is_memory_path(request.url.path):
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if isinstance(body, dict):
                try:
                    before = _compact(body)
                    capture = await gw.transform(body, user_id)
                    after = _compact(body)
                    if after != before:  # untouched requests are forwarded byte-for-byte
                        raw = after.encode("utf-8")
                except Exception as e:  # noqa: BLE001 - never break the request
                    log.warning("transform skipped: %s", type(e).__name__)
        up = None
        for client in (request.app.state.http, request.app.state.fallback):
            if client is None:
                continue
            try:
                up = await client.send(client.build_request(
                    request.method, request.url.path, params=request.query_params,
                    headers=headers, content=raw), stream=True)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Only connection failures fall through: nothing was sent, so a retry
                # against the fallback can never double-bill a request.
                if client is not request.app.state.http:
                    return JSONResponse({"error": f"unreachable: {type(e).__name__}"}, status_code=502)
                gw.fallbacks += 1
            except httpx.HTTPError as e:
                return JSONResponse({"error": f"upstream error: {type(e).__name__}"}, status_code=502)
        if up is None:
            return JSONResponse({"error": "upstream unreachable"}, status_code=502)
        if capture and up.status_code < 300:
            gw.commit(capture, user_id)
        out = {k: v for k, v in up.headers.items() if k.lower() not in HOP}
        # aiter_raw: bytes are forwarded exactly as sent, so the upstream
        # content-encoding / content-length headers stay truthful.
        return StreamingResponse(up.aiter_raw(), status_code=up.status_code, headers=out,
                                 background=BackgroundTask(up.aclose))

    return app


def _compact(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _safe_warm(memory: MemoryService) -> None:
    try:
        memory.warm()
        log.info("mem0 ready")
    except Exception as e:  # noqa: BLE001
        log.warning("mem0 warmup failed (will retry lazily): %s", type(e).__name__)
