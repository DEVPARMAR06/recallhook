"""Claude Code hooks: recall memories on each prompt, learn from Claude's reply each turn.

UserPromptSubmit: asks the gateway for relevant memories and prints them as extra context.
Stop:             sends Claude's final reply to the gateway, which extracts project knowledge.

Stdlib only (it starts on every prompt). Fail-open: if the gateway is down or slow it prints
nothing and exits 0, so it can never block or break a prompt."""
from __future__ import annotations

import json
import sys
import urllib.request

TIMEOUT = 4.0


def _post(port: int, path: str, body: dict) -> dict | None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/_recallhook/api/{path}", data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", "x-hm-client": "claude-hook"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 - gateway down/slow: just skip memory
        return None


def run(port: int | None = None) -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0
    event = data.get("hook_event_name")
    if event not in ("UserPromptSubmit", "Stop"):
        return 0
    if not port:
        from .config import effective_port  # env, then ~/.recallhook/config.json, then 8788

        port = effective_port()
    if event == "Stop":
        if data.get("stop_hook_active"):  # a Stop hook already continued this turn: don't loop
            return 0
        text = data.get("last_assistant_message") or ""
        if text:
            _post(port, "turn", {"text": text, "session": data.get("session_id"), "cwd": data.get("cwd")})
        return 0  # nothing to print: a Stop hook must not steer the conversation
    resp = _post(port, "prompt", {"text": data.get("prompt", ""), "session": data.get("session_id"),
                                  "cwd": data.get("cwd")})
    block = (resp or {}).get("block", "")
    if block:
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0
