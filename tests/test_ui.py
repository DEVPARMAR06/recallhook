import json

import httpx
from fastapi.testclient import TestClient

from recallhook.config import Config
from recallhook.events import EventLog
from recallhook.gateway import create_app

H = {"x-hm-client": "test"}


class Mem:
    def __init__(self, tmp_path):
        self.stats, self.deleted, self.captured, self.meta = {}, [], [], []
        self.events = EventLog(tmp_path / "events.db")
        self._q = type("Q", (), {"qsize": staticmethod(lambda: 0)})()
        self.items = [{"id": "m1", "memory": "User uses pnpm", "score": 0.8}]

    def start(self): ...
    def warm(self): ...
    def problems(self): return ["Model 'x' is not downloaded. Run: ollama pull x"]
    def list_all(self, user): return [{"id": "m1", "memory": "User uses pnpm", "created_at": None, "updated_at": None}]
    def search_scored(self, q, user): return self.items
    def delete(self, mid): self.deleted.append(mid)

    async def recall(self, q, user, source="hook"):
        self.events.add("recall", source, hits=len(self.items), ms=5, tokens=10, detail="x")
        return self.items

    def capture(self, text, user, source="prompt", cwd=None):
        self.captured.append((text, user))
        self.meta.append((source, cwd))
        return None  # None = queued for extraction


def client(mem, **cfg_kw):
    cfg = Config(port=8788, fallback="", upstream="http://127.0.0.1:9", min_chars=5, **cfg_kw)
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
    with client(mem, capture_mode="strict") as c:
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
        assert s["problems"] == ["Model 'x' is not downloaded. Run: ollama pull x"]
        kinds = {e["kind"] for e in c.get("/_recallhook/api/activity").json()}
        assert {"recall", "skipped"} <= kinds  # the question was recalled for, and skipped for storage
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


def test_skipped_prompts_log_only_the_reason_never_the_text(tmp_path):
    mem = Mem(tmp_path)
    secret = "supersecret-project-codename"
    with client(mem, capture_mode="strict") as c:
        def post(text):
            return c.post("/_recallhook/api/prompt", json={"text": text, "session": "k"}, headers=H)

        post(f"Which port does {secret} use for its dev server?")
        post(f"Refactor the {secret} auth module to use async functions")
        post("[SYSTEM NOTIFICATION - NOT USER INPUT] background task finished ok")
        post("/clear")
        post(f"I always deploy {secret} with Docker Compose")  # a lasting statement: queued, not skipped
        summary = c.get("/_recallhook/api/summary").json()
        feed = c.get("/_recallhook/api/activity?limit=50").json()
    assert summary["skipped"] == {"question": 1, "task or request (no lasting fact)": 1,
                                  "automated message": 1, "too short or a command": 1}
    assert [m[0] for m in mem.captured] == [f"I always deploy {secret} with Docker Compose"]
    assert secret not in json.dumps(feed) and secret not in json.dumps(summary)


def test_capture_explains_why_it_declined(tmp_path):
    from recallhook.memory import MemoryService

    cfg = Config(queue_size=1)
    svc = MemoryService(cfg, factory=lambda c: None)
    assert svc.capture("too short", "u") == "too short or a command"
    assert svc.capture("I always use pnpm for every JavaScript project", "u") is None
    assert svc.capture("I always use pnpm for every JavaScript project", "u") == "already stored"
    # a backed-up queue drops the OLDEST work and keeps the newest (the latest reply is the most informative)
    assert svc.capture("My project Atlas deploys to Fly.io in the fra region", "u") is None
    assert svc.stats["dropped"] == 1 and svc._q.qsize() == 1
    assert "Fly.io" in svc._q.get_nowait()[0]
    cfg.write = False
    assert svc.capture("My project Atlas runs on Postgres 16 in development", "u") == "capture is turned off"


# -- smart capture (default) and learning from Claude's replies ------------------------------
def test_smart_mode_sends_tasks_to_extraction_but_skips_short_lookups(tmp_path):
    mem = Mem(tmp_path)
    task = "The checkout service is in services/checkout. Please add retries around the charge call."
    long_q = ("Here is our setup: Postgres 16 on port 5433, Redis on 6391, deployed to Fly.io. " * 3
              + "Why would the pool time out?")
    with client(mem) as c:  # smart is the default
        def post(text):
            return c.post("/_recallhook/api/prompt", json={"text": text, "session": "k", "cwd": "C:/work/atlas"}, headers=H)

        post("Which port does the dev server use?")          # short lookup: skipped
        post(task)                                             # task with project knowledge: extracted
        post(long_q)                                           # long question with context: extracted
        summary = c.get("/_recallhook/api/summary").json()
    assert [t for t, _ in mem.captured] == [task, long_q]
    assert mem.meta == [("prompt", "C:/work/atlas")] * 2       # the project folder travels with it
    assert summary["skipped"] == {"short question (a lookup)": 1}


def test_turn_endpoint_learns_from_claude_replies(tmp_path):
    mem = Mem(tmp_path)
    reply = ("I looked through the repo. The backend is a FastAPI app in src/api using SQLAlchemy with "
             "Postgres 16. Tests run with pytest -q and CI is a GitHub Actions workflow.")
    with client(mem) as c:
        def turn(text):
            return c.post("/_recallhook/api/turn", json={"text": text, "session": "t", "cwd": "C:/work/atlas"}, headers=H).json()

        assert turn(reply) == {"queued": True}
        assert turn("Done! Fixed it.") == {"queued": False}                       # too short to carry knowledge
        wrapped = "<recalled_memories>\n- secret memory\n</recalled_memories>\n" + reply + " Extra detail. " * 3
        assert turn(wrapped) == {"queued": True}
        assert "secret memory" not in mem.captured[-1][0]                         # our own block is never re-captured
        assert turn("[SYSTEM NOTIFICATION - NOT USER INPUT] " + reply) == {"queued": False}
        assert c.post("/_recallhook/api/turn", json={"text": reply}).status_code == 403   # needs the client header
        skipped = c.get("/_recallhook/api/summary").json()["skipped"]
    assert mem.meta == [("turn", "C:/work/atlas")] * 2
    assert skipped == {"assistant reply too short": 1, "automated message": 1}


def test_turn_capture_can_be_switched_off(tmp_path):
    mem = Mem(tmp_path)
    with client(mem, capture_assistant=False) as c:
        r = c.post("/_recallhook/api/turn", json={"text": "x" * 400}, headers=H).json()
        assert r == {"queued": False} and mem.captured == []
        assert c.get("/_recallhook/api/summary").json()["skipped"] == {"assistant capture is off": 1}


# -- "forget" and memory age ---------------------------------------------------------------------------
def test_forget_deletes_the_matching_memory_and_never_stores_the_request(tmp_path):
    mem = Mem(tmp_path)
    with client(mem) as c:
        r = c.post("/_recallhook/api/prompt", json={"text": "Forget that the team uses pnpm", "session": "f"}, headers=H).json()
    assert mem.deleted == ["m1"] and "User uses pnpm" in r["block"] and "deleted" in r["block"]
    assert mem.captured == []                                     # the instruction itself is not a memory
    assert any(e["kind"] == "forgot" for e in mem.events.recent())


def test_forget_with_no_good_match_deletes_nothing(tmp_path):
    mem = Mem(tmp_path)
    mem.items = [{"id": "m1", "memory": "User uses pnpm", "score": 0.3}]   # nothing close enough
    with client(mem) as c:
        r = c.post("/_recallhook/api/prompt", json={"text": "Forget that we deploy to Mars", "session": "f"}, headers=H).json()
    assert mem.deleted == [] and "nothing was deleted" in r["block"]


def test_stale_memories_are_labelled_with_their_age(tmp_path):
    from datetime import datetime, timedelta, timezone

    mem = Mem(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    mem.items = [{"id": "m1", "memory": "Old fact about the build", "score": 0.9, "updated_at": old},
                 {"id": "m2", "memory": "Fresh fact about the build", "score": 0.9,
                  "updated_at": datetime.now(timezone.utc).isoformat()}]
    with client(mem) as c:
        block = c.post("/_recallhook/api/prompt", json={"text": "tell me about the build setup", "session": "a"},
                       headers=H).json()["block"]
    assert "Old fact about the build (noted 3 weeks ago)" in block
    assert "Fresh fact about the build" in block and "Fresh fact about the build (" not in block


# -- the dashboard page itself ---------------------------------------------------------------------------
def test_dashboard_script_has_no_raw_line_breaks_inside_string_literals():
    """A stray newline inside a JS string silently kills the whole dashboard, so scan the script."""
    from recallhook.ui import PAGE

    script = PAGE[PAGE.index("<script>") + 8:PAGE.index("</script>")]
    script = script.replace('/[&<>"]/g', "RE")   # the page's one regex literal contains a quote character
    quote, i, line = None, 0, 1
    while i < len(script):
        ch = script[i]
        if ch == chr(92) and quote:     # a backslash escapes the next character
            i += 2
            continue
        if ch == chr(10):
            assert quote in (None, "`"), f"line break inside a {quote} string near script line {line}"
            line += 1
        elif quote is None and ch in "'\"`":
            quote = ch
        elif quote == ch:
            quote = None
        i += 1
    assert quote is None, "unterminated string in the dashboard script"


def test_restore_puts_an_old_memory_back_and_logs_it(tmp_path):
    mem = Mem(tmp_path)
    restored = []
    mem.restore = lambda text, user: restored.append((text, user))
    with client(mem) as c:
        body = {"text": "The dev server for atlas listens on port 4310"}
        assert c.post("/_recallhook/api/restore", json=body).status_code == 403            # needs the client header
        assert c.post("/_recallhook/api/restore", json=body, headers=H).json() == {"restored": True}
        assert c.post("/_recallhook/api/restore", json={"text": "  "}, headers=H).status_code == 400
        assert c.post("/_recallhook/api/restore", json={"text": "x" * 3000}, headers=H).status_code == 400
    assert restored == [("The dev server for atlas listens on port 4310", "default")]
    assert any(e["kind"] == "restored" for e in mem.events.recent())
