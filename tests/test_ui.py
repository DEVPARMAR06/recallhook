import httpx
from fastapi.testclient import TestClient

from recallhook.config import Config
from recallhook.events import EventLog
from recallhook.gateway import create_app

H = {"x-hm-client": "test"}


class Mem:
    def __init__(self, tmp_path):
        self.stats, self.deleted, self.captured = {}, [], []
        self.events = EventLog(tmp_path / "events.db")
        self._q = type("Q", (), {"qsize": staticmethod(lambda: 0)})()
        self.items = [{"id": "m1", "memory": "User uses pnpm", "score": 0.8}]

    def start(self): ...
    def warm(self): ...
    def list_all(self, user): return [{"id": "m1", "memory": "User uses pnpm", "created_at": None, "updated_at": None}]
    def search_scored(self, q, user): return self.items
    def delete(self, mid): self.deleted.append(mid)

    async def recall(self, q, user, source="hook"):
        self.events.add("recall", source, hits=len(self.items), ms=5, tokens=10, detail="x")
        return self.items

    def capture(self, text, user):
        self.captured.append((text, user))
        return True


def client(mem):
    cfg = Config(port=8788, fallback="", upstream="http://127.0.0.1:9", min_chars=5)
    return TestClient(create_app(cfg, mem, transport=httpx.MockTransport(lambda r: httpx.Response(200))),
                      base_url="http://127.0.0.1:8788")


def test_ui_and_memory_api(tmp_path):
    mem = Mem(tmp_path)
    with client(mem) as c:
        assert "RECALLHOOK" in c.get("/_recallhook/ui").text
        assert c.get("/_recallhook/api/memories").json()[0]["memory"] == "User uses pnpm"
        assert c.get("/_recallhook/api/search", params={"q": "js"}).json()[0]["score"] == 0.8
        assert c.delete("/_recallhook/api/memories/m1").status_code == 403  # needs client header
        assert c.delete("/_recallhook/api/memories/m1", headers=H).json() == {"deleted": "m1"}
    assert mem.deleted == ["m1"]


def test_foreign_host_is_rejected(tmp_path):
    with client(Mem(tmp_path)) as c:
        assert c.get("/_recallhook/api/memories", headers={"host": "evil.example:8788"}).status_code == 403
        r = c.post("/_recallhook/api/prompt", json={"text": "hello world"}, headers={**H, "host": "evil.example"})
        assert r.status_code == 403


def test_prompt_endpoint_recalls_dedups_per_session_and_captures(tmp_path):
    mem = Mem(tmp_path)
    with client(mem) as c:
        body = {"text": "which package manager do I use?", "session": "s1"}
        first = c.post("/_recallhook/api/prompt", json=body, headers=H).json()
        assert first["hits"] == 1 and "User uses pnpm" in first["block"]
        again = c.post("/_recallhook/api/prompt", json=body, headers=H).json()
        assert again == {"block": "", "hits": 0}  # same session: not injected twice
        other = c.post("/_recallhook/api/prompt", json={**body, "session": "s2"}, headers=H).json()
        assert other["hits"] == 1  # a new session gets it again
        assert c.post("/_recallhook/api/prompt", json=body).status_code == 403  # no client header
        skip = c.post("/_recallhook/api/prompt", json={"text": "/clear"}, headers=H).json()
        assert skip["block"] == ""
        c.post("/_recallhook/api/prompt", json={"text": "What port does it use? One short sentence.", "session": "s4"}, headers=H)
        c.post("/_recallhook/api/prompt", json={"text": "I always deploy with Docker Compose", "session": "s3"}, headers=H)
    # questions are recalled for but never captured, and slash commands are ignored entirely
    assert mem.captured == [("I always deploy with Docker Compose", "default")]


def test_dashboard_data_endpoints(tmp_path):
    mem = Mem(tmp_path)
    with client(mem) as c:
        c.post("/_recallhook/api/prompt", json={"text": "what do I use for JS?", "session": "a"}, headers=H)
        s = c.get("/_recallhook/api/summary").json()
        assert s["recalls"] == 1 and s["hit_rate"] == 100 and s["memories"] == 1
        assert c.get("/_recallhook/api/activity").json()[0]["kind"] == "recall"
        assert c.get("/_recallhook/api/series").json()[0]["recall"] == 1


def test_only_durable_statements_are_worth_capturing():
    from recallhook.inject import looks_durable

    yes = ["My project Atlas runs its unit tests with pnpm test:unit.",
           "For Atlas I name git branches like feat/ABC-123-slug",
           "Atlas uses PostgreSQL 16 on port 5433 in development.",
           "I always use tabs, never spaces.", "Remember that staging is on Fly.io"]
    no = ["Write a Python one-liner that reverses a string. Just the code.",
          "Explain in two sentences what a mutex is.", "Fix the failing test in utils.py",
          "Refactor this function to be shorter", "run the tests"]
    assert all(looks_durable(t) for t in yes)
    assert not any(looks_durable(t) for t in no)


def test_wrapped_and_automated_prompts(tmp_path):
    mem = Mem(tmp_path)
    queries = []
    orig = mem.recall

    async def spy(q, user, source="hook"):
        queries.append(q)
        return await orig(q, user, source)

    mem.recall = spy
    wrapper = "<system-reminder>\n" + "scratch folder boilerplate. " * 120 + "\n</system-reminder>\n\n"
    with client(mem) as c:
        # app wrapper first, the real ask last: recall must search on the ask, not the wrapper
        r = c.post("/_recallhook/api/prompt", headers=H,
                   json={"text": wrapper + "Task:\nwhich package manager do we use here?", "session": "w"})
        assert r.json()["hits"] == 1
        assert queries == ["Task:\nwhich package manager do we use here?"]
        # automated "user" turns are never recalled for or captured
        n = len(queries)
        for t in ("[SYSTEM NOTIFICATION - NOT USER INPUT] The session runs on its own and I always...",
                  "<task-notification><summary>It runs on port 80</summary></task-notification> remember this"):
            assert c.post("/_recallhook/api/prompt", headers=H, json={"text": t, "session": "x"}).json()["block"] == ""
        assert len(queries) == n and mem.captured == []


def test_recall_query_keeps_both_ends_of_long_prompts():
    from recallhook.inject import recall_query

    long = "HEAD" + "x" * 3000 + "TAIL"
    q = recall_query(long)
    assert q.startswith("HEAD") and q.endswith("TAIL") and len(q) <= 1001
    assert recall_query("short") == "short"
