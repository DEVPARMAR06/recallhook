"""Pure request-body helpers: find real user text, inject recalled memories.

Works on both Anthropic (/v1/messages) and OpenAI (/v1/chat/completions) bodies.
Nothing here does I/O, so it is trivially unit-testable.
"""
from __future__ import annotations

import hashlib
import re

MEMORY_PATHS = ("/v1/messages", "/v1/chat/completions")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_OURS = re.compile(r"<recalled_memories>.*?</recalled_memories>", re.DOTALL)
TAG_OPEN = "<recalled_memories>"

# A prompt is only worth storing if it states something lasting about the user or a
# project. Plain task requests ("write a one-liner...") otherwise become junk memories.
_DURABLE = re.compile(
    r"\b(i|we)\s+(always|never|usually|prefer|use|like|hate|work|run|deploy|name|call|write|keep|avoid)\b"
    r"|\b(my|our)\s+(project|repo|repository|team|stack|setup|server|app|company|workflow|name|preference)\b"
    r"|\b(remember|from now on|going forward|my convention|our convention)\b"
    r"|\b(always|never)\s+(use|run|write|name|deploy|commit|format|lint)\b"
    r"|\bis called\b|\blistens on port\b|\bruns on port\b",
    re.IGNORECASE)
_NAMED_SUBJECT = re.compile(r"\b[A-Z][\w-]+(?:'s)?\s+(uses?|runs?|listens?|deploys?|is deployed|is written|has)\b")
# Messages the app/harness injects as "user" turns. They are not the user speaking:
# never recall for them and never store them.
_AUTOMATED = re.compile(
    r"SYSTEM NOTIFICATION|<task-notification|<side-sessions-event|<ci-monitor-event"
    r"|\[Artifact comment|<command-name>|<local-command", re.IGNORECASE)


def looks_durable(text: str) -> bool:
    return bool(_DURABLE.search(text) or _NAMED_SUBJECT.search(text))


def capture_skip_reason(text: str) -> str | None:
    """Why this prompt is not worth storing, or None if it is a lasting statement.
    The wording is shown in the dashboard, so keep it plain."""
    if is_question(text):
        return "question"
    if not looks_durable(text):
        return "task or request (no lasting fact)"
    return None


def is_automated(text: str) -> bool:
    return bool(_AUTOMATED.search(text))


def clean_prompt(text: str) -> str:
    """The user's own words: harness <system-reminder> wrappers and our own blocks removed."""
    return _OURS.sub("", _REMINDER.sub("", text)).strip()


def recall_query(text: str, limit: int = 1000) -> str:
    """Query text for recall. Long prompts keep their start and end (pasted logs put the
    ask at one end, wrapper text at the other) instead of just the first `limit` chars."""
    return text if len(text) <= limit else f"{text[: limit // 2]} {text[-(limit // 2):]}"


def is_memory_path(path: str) -> bool:
    return path.rstrip("/") in MEMORY_PATHS


def is_side_request(body: dict) -> bool:
    """Tiny background calls (quota probes, title generation) never get memory."""
    mt = body.get("max_tokens")
    return isinstance(mt, int) and mt <= 32


def real_text(message: dict) -> str:
    """User-authored text of a message: no tool results, no system reminders."""
    content = message.get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
    else:
        return ""
    text = "\n".join(parts)
    text = _OURS.sub("", _REMINDER.sub("", text))
    return text.strip()


def text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def user_turns(body: dict) -> list[tuple[int, str]]:
    """(message index, real text) for each user message that has real text."""
    out = []
    for i, m in enumerate(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            t = real_text(m)
            if t:
                out.append((i, t))
    return out


def is_question(text: str) -> bool:
    """A short prompt that asks something (contains a question mark). Capturing it makes mem0 restate
    already-known facts as near-duplicate memories, so such prompts are recalled for but
    never stored."""
    t = text.strip()
    return "?" in t and len(t) < 400


def format_block(memories: list[str]) -> str:
    lines = "\n".join(f"- {m}" for m in memories)
    return (
        f"{TAG_OPEN}\nLong-term memory about the user/project from earlier sessions "
        f"(may be outdated; the user's current message takes priority):\n{lines}\n</recalled_memories>"
    )


def append_block(message: dict, block: str) -> None:
    content = message.get("content")
    if isinstance(content, str):
        message["content"] = f"{content}\n\n{block}"
    elif isinstance(content, list):
        content.append({"type": "text", "text": block})
