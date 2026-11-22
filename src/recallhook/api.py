"""JSON API used by the Claude Code hook and the dashboard."""
from __future__ import annotations

import asyncio
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .inject import (age_label, capture_skip_reason, clean_prompt, forget_request, format_block, has_specifics,
                     is_automated, recall_query)
from .security import make_guard


class PromptIn(BaseModel):
    text: str
    user: str | None = None
    session: str | None = None
    cwd: str | None = None


class TurnIn(BaseModel):
    """Claude's final reply for a turn (from the Stop hook)."""
    text: str
    user: str | None = None
    session: str | None = None
    cwd: str | None = None


class RestoreIn(BaseModel):
    text: str
    user: str | None = None


MIN_TURN_CHARS = 150  # shorter replies ("Done!", "OK, fixed") carry no project knowledge
MAX_TURN_CHARS = 6000


def build_api_router(cfg, memory) -> APIRouter:
    r = APIRouter(prefix="/_recallhook/api")
    guard = make_guard(cfg)
    # memories already injected per Claude session: injecting the same fact on every
    # prompt would just burn tokens, since earlier injections stay in the transcript.
    seen: OrderedDict[str, set[str]] = OrderedDict()

    async def forget(wanted: str, user: str) -> dict:
        """Delete the memories that best match what the user asked to forget (never more than 3)."""
        found = await asyncio.to_thread(memory.search_scored, wanted[:500], user)
        hits = [m for i, m in enumerate(found) if m["score"] >= (0.6 if i == 0 else 0.75)][:3]
        for m in hits:
            await asyncio.to_thread(memory.delete, m["id"])
        memory.events.add("forgot", "hook", hits=len(hits))
        if not hits:
            msg = "recallhook: no stored memory matched what the user asked to forget, so nothing was deleted."
        else:
            msg = ("recallhook: at the user's request, these memories were deleted: "
                   + "; ".join(m["memory"] for m in hits) + ". Confirm this to the user in one short sentence.")
        return {"block": msg, "hits": len(hits)}

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
        wanted = forget_request(text)
        if wanted:  # "forget that ..." is an explicit instruction: delete the matching memories, never store it
            return await forget(wanted, user)
        items = await memory.recall(recall_query(text), user, source="hook") if cfg.inject else []
        items = items or []
        sid = body.session or ""
        known = seen.setdefault(sid, set())
        seen.move_to_end(sid)
        while len(seen) > 200:
            seen.popitem(last=False)
        fresh = [i for i in items if i["id"] not in known]
        known.update(i["id"] for i in fresh)
        reason = capture_skip_reason(text, cfg.capture_mode) or memory.capture(text, user, "prompt", body.cwd)
        if reason:
            skipped(reason)
        lines = [i["memory"] + (f" (noted {a})" if (a := age_label(i.get("updated_at") or i.get("created_at"))) else "")
                 for i in fresh]  # old memories carry their age so a stale one is visibly stale
        return {"block": format_block(lines) if fresh else "", "hits": len(fresh)}

    @r.post("/turn")
    async def turn(request: Request, body: TurnIn):
        """Learn from Claude's reply: project knowledge mostly surfaces there, not in prompts."""
        guard(request, mutating=True)
        user = body.user or cfg.user_id

        def skipped(reason: str) -> None:  # the reason only, never the text
            memory.events.add("skipped", "turn", detail=reason)

        if not cfg.capture_assistant:
            skipped("assistant capture is off")
            return {"queued": False}
        text = clean_prompt(body.text)  # drops harness reminders and our own recalled-memory block
        if is_automated(body.text):
            skipped("automated message")
            return {"queued": False}
        if len(text) < MIN_TURN_CHARS:
            skipped("assistant reply too short")
            return {"queued": False}
        if not has_specifics(text):  # no path, file, command, port or version: nothing concrete to learn
            skipped("assistant reply has no project details")
            return {"queued": False}
        reason = memory.capture(text[:MAX_TURN_CHARS], user, "turn", body.cwd)
        if reason:
            skipped(reason)
        return {"queued": reason is None}

    @r.post("/restore")
    async def restore(request: Request, body: RestoreIn):
        """Undo a replacement: put an old memory back (the dashboard's 'restore old' button)."""
        guard(request, mutating=True)
        text = body.text.strip()
        if not text or len(text) > 2000:
            raise HTTPException(400, "nothing to restore")
        await asyncio.to_thread(memory.restore, text, body.user or cfg.user_id)
        memory.events.add("restored", "ui", hits=1, detail=text[:300])
        return {"restored": True}

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
