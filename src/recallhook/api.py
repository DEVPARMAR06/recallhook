"""JSON API used by the Claude Code hook and the dashboard."""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .inject import capture_skip_reason, clean_prompt, format_block, is_automated, recall_query
from .security import make_guard


class PromptIn(BaseModel):
    text: str
    user: str | None = None
    session: str | None = None


def build_api_router(cfg, memory) -> APIRouter:
    r = APIRouter(prefix="/_recallhook/api")
    guard = make_guard(cfg)
    # memories already injected per Claude session: injecting the same fact on every
    # prompt would just burn tokens, since earlier injections stay in the transcript.
    seen: OrderedDict[str, set[str]] = OrderedDict()

    @r.post("/prompt")
    async def prompt(request: Request, body: PromptIn):
        """One round trip for the hook: recall relevant memories, then queue capture."""
        guard(request, mutating=True)
        user = body.user or cfg.user_id

        def skipped(reason: str) -> None:  # the reason only, never the prompt text
            memory.events.add("skipped", "hook", detail=reason)

        if is_automated(body.text):  # app/harness messages, not the user
            skipped("automated message")
            return {"block": "", "hits": 0}
        text = clean_prompt(body.text)
        if len(text) < cfg.min_chars or text.startswith("/"):
            skipped("too short or a command")
            return {"block": "", "hits": 0}
        items = await memory.recall(recall_query(text), user, source="hook") if cfg.inject else []
        items = items or []
        sid = body.session or ""
        known = seen.setdefault(sid, set())
        seen.move_to_end(sid)
        while len(seen) > 200:
            seen.popitem(last=False)
        fresh = [i for i in items if i["id"] not in known]
        known.update(i["id"] for i in fresh)
        reason = capture_skip_reason(text) or memory.capture(text, user)  # None = queued for extraction
        if reason:
            skipped(reason)
        return {"block": format_block([i["memory"] for i in fresh]) if fresh else "", "hits": len(fresh)}

    @r.get("/activity")
    async def activity(request: Request, limit: int = 40):
        guard(request)
        return memory.events.recent(min(limit, 200))

    @r.get("/series")
    async def series(request: Request, days: int = 14):
        guard(request)
        return memory.events.series(min(days, 90))

    @r.get("/summary")
    async def summary(request: Request):
        guard(request)
        t = memory.events.totals()
        try:
            t["memories"] = len(await asyncio.to_thread(memory.list_all, cfg.user_id))
        except Exception:  # noqa: BLE001
            t["memories"] = None
        t["capture_queue"] = memory._q.qsize()
        t["failures"] = memory.stats.get("search_failures", 0)
        t["skipped"] = memory.events.skipped()
        t["problems"] = await asyncio.to_thread(getattr(memory, "problems", lambda: []))
        return t

    return r
