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


def capture_skip_reason(text: str, mode: str = "smart") -> str | None:
    """Why this prompt is not worth sending to fact extraction, or None if it is.
    The wording is shown in the dashboard, so keep it plain.

    smart (default): extract from nearly everything, since project knowledge also hides in
    task requests ("the checkout service is in services/checkout, add retries to it"); only
    short lookup questions are skipped. Narration is filtered out after extraction.
    strict: only lasting statements ("I always...", "my project X uses...") are extracted."""
    if mode == "strict":
        if is_question(text):
            return "question"
        if not looks_durable(text):
            return "task or request (no lasting fact)"
        return None
    if "?" in text and len(text) <= 200:
        return "short question (a lookup)"
    if not (looks_durable(text) or has_specifics(text)):
        return "no project details to learn from"  # a vague task makes a small model invent facts
    return None


# -- grounding: every memory must be supported by the text it was extracted from ---------------
_EXT = r"(?:py|js|ts|tsx|jsx|json|ya?ml|toml|md|env|go|rs|java|rb|php|sql|sh|cfg|ini|lock|txt|html|css)"
_SPECIFICS = re.compile(
    r"[\w.-]+/[\w./-]+"                      # paths: src/api, services/checkout
    r"|\b[\w-]+\." + _EXT + r"\b"            # file names: package.json, render.yaml
    r"|`[^`\n]+`"                            # backticked commands / identifiers
    r"|\b\d{3,}\b|\bport\s+\d+|\bv?\d+\.\d+\b"  # ports, versions
    r"|\b[A-Z][A-Z0-9_]{3,}\b")              # ENV_VARS
_STOP = frozenset(
    "this that with from have been were will would their there where which while also only each every such these "
    "those being into over than then them they what when your about after before using used uses user users "
    "project application file files folder directory command commands runs running configured located called "
    "named name notes coding assistant made make "
    "package packages manager management framework system tool tools library language convention conventions "
    "follows follow instead enforce enforces ensure indentation formatting style approach default "
    "and for the its has are was not can any all but you our one two get set use how why who may now new old out "
    "per via yet too own via".split())


def has_specifics(text: str) -> bool:
    """True if the text names something concrete (a path, file, command, port, version, env var)."""
    return bool(_SPECIFICS.search(text))


def _concrete_tokens(memory: str) -> list[str]:
    toks = []
    for raw in re.split(r"[\s(),;\"']+", memory):
        tok = raw.strip("`.:;[]{}")
        if len(tok) > 1 and re.search(r"[/_:]|\.\w|\d{2,}", tok):
            toks.append(tok.lower())
    return toks


def is_grounded(memory: str, source: str) -> bool:
    """False when a memory contains details its source never mentioned (the model copied or
    invented them): every concrete token (number, path, file, command) must appear in the source,
    and at least 40% of the other content words must too (prefix match tolerates inflection)."""
    src = source.lower()
    if any(tok not in src for tok in _concrete_tokens(memory)):
        return False
    # "The project <name> ...": the model reuses the project name from existing memories and pins a
    # new fact on it. A name (hyphenated/dotted, or capitalised) must come from the source or its folder.
    m = re.match(r"\s*(?:the\s+)?(?:project|repo|repository|app|service)\s+([\w.-]+)", memory, re.IGNORECASE)
    if m and re.search(r"[-_.]|^[A-Z]", m.group(1)) and m.group(1).lower() not in src:
        return False
    # a quoted command or identifier ('mvn test', `pnpm dev`) is a concrete claim too
    if any(q.lower() not in src for q in re.findall(r"`([^`\n]+)`|'([^'\n]{2,})'", memory) for q in q if q):
        return False
    # the project folder name is added by us, so it must not count as support for a memory
    named = {w for m in re.findall(r"\(project folder: ([^)\n]+)\)", src) for w in re.findall(r"[a-z]{3,}", m)}
    words = [w for w in re.findall(r"[a-z]{3,}", memory.lower()) if w not in _STOP and w not in named]
    if not words:
        return True
    return sum(1 for w in words if w[:5] in src) / len(words) >= 0.4


# Things extraction wrongly records even with good instructions: what the user asked for,
# what was just done, or a failing state. Those are narration, not knowledge about a project.
_NARRATION = re.compile(
    r"\b(the\s+)?(user|assistant|developer|coder)(?:'s)?\s+(has\s+|had\s+)?"
    r"(asked|requested|wants?|wanted|wrote|written|ran|run|fixed|fixes|increased|decreased|changed|added|"
    r"removed|updated|identified|found|noticed|reported|tried|created|implemented|started|finished|"
    r"is\s+(asking|trying|working|looking|fixing|debugging|running)|needs?\s+to|would\s+like|"
    r"has\s+an?\s+(failing|broken|error|issue|bug)|cannot|can't)\b"
    r"|\brequires?\s+(adding|fixing|updating|implementing|removing|refactoring|writing)\b"
    r"|\b(is|are|was|were)\s+(currently\s+)?(failing|broken|crashing)\b|\bfailing\s+tests?\b"
    r"|\bto\s+fix\s+an?\s+(bug|issue|error)\b|\b(will|should)\s+(look\s+into|investigate)\b",
    re.IGNORECASE)


# Snapshots of work in progress: they go stale the moment the problem is fixed, and recallhook can't
# tell when that happens. "has 46 files that may be incorrectly filed", "631 videos not yet copied".
_SNAPSHOT = re.compile(
    r"\b(has|have|contains?|with)\s+\d[\d,]*\s+(?:[\w-]+\s+){0,2}"
    r"(files?|videos?|photos?|images?|items?|rows?|records?|errors?|warnings?|failures?|issues?|bugs?|tasks?|"
    r"todos?|commits?|folders?|documents?|entries|tests?|messages?|users?|pages?|duplicates?|conflicts?)\b"
    r"|\b(may|might|could)\s+(be|need|have|require)\b|\bnot\s+yet\b|\bincorrectly\b"
    r"|\b(pending|unresolved|outstanding|in\s+progress)\b"
    r"|\bneeds?\s+to\s+be\s+(reviewed|fixed|moved|copied|checked|verified|migrated|consolidated)\b",
    re.IGNORECASE)


def is_ephemeral(memory: str) -> bool:
    """True for 'memories' that narrate the conversation or snapshot work in progress, instead of
    describing how the project is set up."""
    return bool(_NARRATION.search(memory) or _SNAPSHOT.search(memory))


def age_label(iso: str | None) -> str | None:
    """'2 weeks ago' for memories a week or more old (younger ones need no label), else None."""
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - (then if then.tzinfo else then.replace(tzinfo=timezone.utc))).days
    except ValueError:
        return None
    if days < 7:
        return None
    if days < 30:
        w = days // 7
        return f"{w} week{'s' if w > 1 else ''} ago"
    m = days // 30
    return f"{m} month{'s' if m > 1 else ''} ago"


_FORGET = re.compile(r"^\s*(?:please\s+)?forget(?:\s+that|\s+about|:)?\s+(.{6,})$", re.IGNORECASE | re.DOTALL)


def forget_request(text: str) -> str | None:
    """'Forget that the staging DB is on 5433' -> 'the staging DB is on 5433' (what to forget), else None."""
    m = _FORGET.match(text)
    return m.group(1).strip() if m else None


# -- secrets: never send them to the extraction model or keep them in a memory ------------------------------
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[redacted key]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[redacted token]"),
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj|ant)?[-_]?[A-Za-z0-9_-]{20,}"), "[redacted token]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "[redacted token]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9A-Za-z_-]{30,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[redacted token]"),
    (re.compile(r"\b(?:pypi|npm)[-_][A-Za-z0-9_-]{30,}"), "[redacted token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^/\s@]{3,}(@)"), r"\1[redacted]\2"),      # user:password@host
    # NAME=value where NAME ends in password/secret/key/token (DATABASE_PASSWORD, api_key, csrf_token ...)
    (re.compile(r"(?i)([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|"
                r"client[_-]?secret|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"), r"\1[redacted]"),
]


def redact_secrets(text: str) -> str:
    """Mask API keys, tokens, private keys, passwords and credentials in URLs."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def looks_secret(memory: str) -> bool:
    """True if a memory still contains something that looks like a credential (the safety net)."""
    return redact_secrets(memory) != memory


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
