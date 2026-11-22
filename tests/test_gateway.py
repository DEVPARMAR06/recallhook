import json

import httpx
from fastapi.testclient import TestClient

from recallhook.config import Config
from recallhook.gateway import create_app
from recallhook.inject import is_side_request, real_text


class FakeMemory:
    def __init__(self, results=("User prefers Python",)):
        self.results, self.writes, self.queries = results, [], []
        self.stats = {}

    def start(self): ...
    def warm(self): ...

    async def search(self, query, user_id):
        self.queries.append((query, user_id))
        return None if self.results is None else list(self.results)

    def submit(self, text, user_id):
        self.writes.append((text, user_id))
        return True


class Transport(httpx.AsyncBaseTransport):
    """Like MockTransport, but responses stay unread (as with a real network),
    so the gateway's aiter_raw streaming path is what gets exercised."""

    def __init__(self, handler):
        self.handler = handler

    async def handle_async_request(self, request):
        await request.aread()
        r = self.handler(request)
        return httpx.Response(r.status_code, headers=r.headers, stream=httpx.ByteStream(r.content))


def make(mem, handler):
    cfg = Config(upstream="http://up.test", min_chars=5, fallback="")
    return TestClient(create_app(cfg, mem, transport=Transport(handler)))


def echo(req: httpx.Request):
    return httpx.Response(200, json={"seen": json.loads(req.content) if req.content else None,
                                     "user_hdr": req.headers.get("x-recallhook-user"),
                                     "key": req.headers.get("x-api-key")})


def body(text, **kw):
    return {"model": "m", "max_tokens": 1000, "messages": [{"role": "user", "content": text}], **kw}


def test_real_text_skips_reminders_and_tool_results():
    m = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "1", "content": "x"},
        {"type": "text", "text": "<system-reminder>noise</system-reminder>"},
        {"type": "text", "text": "fix the bug"}]}
    assert real_text(m) == "fix the bug"
    assert real_text({"role": "user", "content": [{"type": "tool_result", "content": "x"}]}) == ""


def test_side_request_detected():
    assert is_side_request({"max_tokens": 1}) and not is_side_request({"max_tokens": 4096})


def test_injects_into_newest_user_turn_and_forwards_headers():
    mem = FakeMemory()
    with make(mem, echo) as c:
        r = c.post("/v1/messages", json=body("what language do I like?"),
                   headers={"x-api-key": "k", "x-recallhook-user": "alice"})
    seen = r.json()["seen"]["messages"][0]["content"]
    assert "<recalled_memories>" in seen and "User prefers Python" in seen
    assert r.json()["key"] == "k"  # auth forwarded
    assert r.json()["user_hdr"] is None  # our header not leaked upstream
    assert mem.queries == [("what language do I like?", "alice")]
    assert mem.writes == [("what language do I like?", "alice")]


def test_prefix_stays_identical_across_followup_requests():
    mem = FakeMemory()
    with make(mem, echo) as c:
        first = body("remember my setup please")
        a = c.post("/v1/messages", json=first).json()["seen"]["messages"]
        followup = {**first, "messages": first["messages"] + [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "r"}]}]}
        b = c.post("/v1/messages", json=followup).json()["seen"]["messages"]
    assert b[0] == a[0]  # same injected block, so provider cache prefix is unchanged
    assert len(mem.queries) == 1 and len(mem.writes) == 1  # no re-search / re-write


def test_search_failure_fails_open_and_is_not_cached():
    mem = FakeMemory(results=None)
    with make(mem, echo) as c:
        r = c.post("/v1/messages", json=body("hello there friend"))
    assert r.json()["seen"]["messages"][0]["content"] == "hello there friend"


def test_side_request_and_other_paths_untouched():
    mem = FakeMemory()
    with make(mem, echo) as c:
        small = c.post("/v1/messages", json=body("quota check", max_tokens=1)).json()["seen"]
        other = c.post("/v1/messages/count_tokens", json=body("count me")).json()["seen"]
    assert small["messages"][0]["content"] == "quota check"
    assert other["messages"][0]["content"] == "count me"
    assert mem.queries == [] and mem.writes == []


def test_openai_format_and_stream_passthrough():
    def sse(req):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"data: 1\n\ndata: [DONE]\n\n")
    mem = FakeMemory()
    with make(mem, sse) as c:
        r = c.post("/v1/chat/completions", json={"model": "m", "stream": True, "messages": [
            {"role": "user", "content": [{"type": "text", "text": "plan my week"}]}]})
    assert r.content == b"data: 1\n\ndata: [DONE]\n\n"
    assert r.headers["content-type"].startswith("text/event-stream")


def test_upstream_down_returns_502():
    def boom(req):
        raise httpx.ConnectError("nope")
    with make(FakeMemory(), boom) as c:
        assert c.post("/v1/messages", json=body("hi there")).status_code == 502


def test_capture_only_after_successful_upstream():
    mem = FakeMemory()
    with make(mem, lambda req: httpx.Response(401, json={"error": "auth"})) as c:
        assert c.post("/v1/messages", json=body("remember my setup please")).status_code == 401
    assert mem.writes == []  # rejected request leaves no memory
    ok = FakeMemory()
    with make(ok, echo) as c:
        c.post("/v1/messages", json=body("remember my setup please"))
        c.post("/v1/messages", json=body("remember my setup please"))
    assert ok.writes == [("remember my setup please", "default")]  # once, deduped


def test_connection_failure_falls_back_to_anthropic_without_memory_loss():
    def boom(req):
        raise httpx.ConnectError("headroom is down")

    cfg = Config(upstream="http://up.test", fallback="http://anthropic.test", min_chars=5)
    app = create_app(cfg, FakeMemory(), transport=Transport(boom),
                     fallback_transport=Transport(echo))
    with TestClient(app) as c:
        r = c.post("/v1/messages", json=body("what language do I like?"), headers={"x-api-key": "k"})
        assert r.status_code == 200
        assert r.json()["key"] == "k"
        assert "User prefers Python" in r.json()["seen"]["messages"][0]["content"]
        assert c.get("/_recallhook/health").json()["fallbacks"] == 1


def test_read_errors_do_not_fall_back():
    calls = []

    def reads_then_dies(req):
        raise httpx.ReadTimeout("slow")

    cfg = Config(upstream="http://up.test", fallback="http://anthropic.test", min_chars=5)
    app = create_app(cfg, FakeMemory(), transport=Transport(reads_then_dies),
                     fallback_transport=Transport(lambda r: calls.append(r) or httpx.Response(200)))
    with TestClient(app) as c:
        assert c.post("/v1/messages", json=body("hello there friend")).status_code == 502
    assert calls == []  # never re-sent: a retry could be billed twice
