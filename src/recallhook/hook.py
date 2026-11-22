"""Claude Code UserPromptSubmit hook: recall relevant memories, capture the prompt.

Stdlib only (it starts on every prompt). Fail-open: if the gateway is down or slow
it prints nothing and exits 0, so it can never block or break a prompt."""
from __future__ import annotations

import json
import os
import sys
import urllib.request

TIMEOUT = 4.0


def run(port: int | None = None) -> int:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0
    if data.get("hook_event_name") != "UserPromptSubmit":
        return 0
    port = port or int(os.environ.get("RECALLHOOK_PORT", "8788"))
    body = json.dumps({"text": data.get("prompt", ""), "session": data.get("session_id")}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/_recallhook/api/prompt", data=body,
        headers={"content-type": "application/json", "x-hm-client": "claude-hook"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            block = json.loads(r.read()).get("block", "")
    except Exception:  # noqa: BLE001 - gateway down/slow: just skip memory
        return 0
    if block:
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": block}}
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0
