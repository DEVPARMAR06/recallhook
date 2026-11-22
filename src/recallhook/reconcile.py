"""Retire memories that a newer fact makes false.

recallhook used to only add memories, so when a fact changed (the dev server moved from port 4310 to
4311, the project switched from npm to pnpm) the old memory stayed and contradicted the new one.
After each capture every new memory is compared with the closest existing ones:

1. same_slot: the new memory is the old one with one number changed -> replace (deterministic, no model).
2. supersedes: otherwise the local model is asked one narrow question per close memory: are these the
   same property of the same thing, so only one can be true? Anything but a clear YES keeps the old memory.

Measured on labelled pairs (qwen3:8b): all 7 true updates were recognised and 10 of 11 look-alikes
(Redis vs PostgreSQL, dev vs test server, S3 logs vs uploads...) were kept; the other was a refinement
that already contains the old fact. Asking the model for a *list* of outdated facts was worse: it
retired valid memories. Every removal is logged with its old text so the dashboard can restore it."""
from __future__ import annotations

import re
from typing import Any, Callable

_WORD = re.compile(r"[a-z0-9_.:/-]+")
_NOISE = frozenset("the a an of to in on for and is are was were its it this that with by as at".split())
MAX_REMOVED_PER_FACT = 2   # a new fact never retires more than this many memories
MAX_MODEL_CHECKS = 6       # per capture: bounds the extra background model calls


def _tokens(text: str) -> set[str]:
    return {t.strip(".:") for t in _WORD.findall(text.lower()) if t not in _NOISE and t.strip(".:")}


def same_slot(old: str, new: str) -> bool:
    """True when `new` is `old` with one value changed, and that value is a number/version/port.

    "dev server listens on port 4310" -> "...port 4311" replaces. "dev server ... 4310" vs "api server
    ... 4311" does not (two things differ, so they are different facts), and neither does a changed
    word like dev -> test, which may be a different thing rather than the same thing updated."""
    a, b = _tokens(old), _tokens(new)
    if not a or not b or a == b:
        return False
    only_old, only_new = a - b, b - a
    shared = len(a & b)
    if len(only_old) != 1 or len(only_new) != 1 or shared < 4 or shared / len(a | b) < 0.6:
        return False
    return any(c.isdigit() for c in next(iter(only_old))) and any(c.isdigit() for c in next(iter(only_new)))


PAIR_PROMPT = (
    "Two facts about a software project:\nA: {old}\nB: {new}\n\n"
    "Are A and B about the SAME property of the SAME thing (same component, same environment), so that "
    "once B is true A can no longer be true?\n"
    "Examples:\n"
    "- A 'dev server listens on port 4310', B 'dev server listens on port 4311' -> YES\n"
    "- A 'dev server listens on port 4310', B 'api server listens on port 4311' -> NO\n"
    "- A 'unit tests use vitest', B 'end to end tests use playwright' -> NO\n"
    "- A 'API runs on port 3000 in development', B 'API runs on port 8080 in production' -> NO\n"
    "- A 'project uses yarn', B 'project switched from yarn to pnpm' -> YES\n"
    "Answer with exactly one word: YES or NO."
)


def supersedes(generate: Callable[..., Any], old: str, new: str) -> bool:
    """True only on a clear YES. Never raises: a failed or unclear check keeps the old memory."""
    try:
        raw = generate([{"role": "user", "content": PAIR_PROMPT.format(old=old, new=new)}])
    except Exception:  # noqa: BLE001
        return False
    return isinstance(raw, str) and bool(re.match(r"\s*yes\b", raw, re.IGNORECASE))
