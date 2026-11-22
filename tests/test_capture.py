import pytest

from recallhook.config import Config, parse_setting
from recallhook.inject import capture_skip_reason, is_ephemeral
from recallhook.memory import MemoryService

# Real outputs from the local model (qwen3:8b) in our extraction experiment.
NARRATION = [  # recorded by the model even with good instructions; none is knowledge about a project
    "The user has a failing test in the utils.py file of the project Atlas",
    "The project Atlas requires adding a function that reverses a string",
    "The project Atlas requires adding retries around the charge call in the checkout service",
    "User requested to add a function that reverses a string",
    "The user increased the retry count from 3 to 5 in client.py to fix a bug",
    "The user fixed a bug by increasing the retry count from 3 to 5 in client.py and added a test for it",
    "The test_login, test_refresh, and test_logout tests are failing in the project's test suite",
    "The user ran the tests and identified 3 failed tests: test_login, test_refresh, and test_logout",
]
KNOWLEDGE = [  # the project knowledge we do want to keep
    "The project Atlas runs its unit tests using the command pnpm test:unit",
    "The development server of the project Atlas listens on port 4310",
    "The checkout service of the project Atlas is located in the services/checkout directory",
    "The checkout service of the project Atlas communicates with Stripe via the billing client in lib/billing.ts",
    "The project Atlas follows a coding convention that uses tabs instead of spaces for indentation",
    "The project Atlas enforces commit messages to be written in the imperative mood",
    "The backend of the project is a FastAPI application located in the src/api directory",
    "The backend uses SQLAlchemy with PostgreSQL 16 as the database",
    "The project runs tests using the pytest command with the -q flag for quiet output",
    "The continuous integration (CI) workflow for testing is configured in the .github/workflows/test.yml file",
    "The staging Redis instance is shared and should never be flushed",
    "The Redis configuration was updated to use the allkeys-lru policy to resolve OOM errors",
    "The project uses hatchling for building and publishes to PyPI through GitHub Actions trusted publishing",
    "The project's version is managed in both pyproject.toml and src/pkg/__init__.py, and both must be bumped together",
    "User prefers using tabs over spaces and writes commit messages in the imperative mood",
    "The user advises never to flush the staging Redis",
]


@pytest.mark.parametrize("text", NARRATION)
def test_narration_is_recognised(text):
    assert is_ephemeral(text), text


@pytest.mark.parametrize("text", KNOWLEDGE)
def test_project_knowledge_is_kept(text):
    assert not is_ephemeral(text), text


def test_capture_modes():
    vague = "Refactor the payment module so it uses async functions"
    assert capture_skip_reason(vague) == "no project details to learn from"   # nothing concrete: the model would invent
    assert capture_skip_reason(vague, "strict") == "task or request (no lasting fact)"
    concrete = "The checkout service is in services/checkout, please add retries to the charge call"
    assert capture_skip_reason(concrete) is None                  # smart: a task that names real project details
    assert capture_skip_reason("Which port does Atlas use?") == "short question (a lookup)"
    assert capture_skip_reason("My project Atlas uses pnpm.", "strict") is None


def test_capture_settings_are_validated():
    assert parse_setting("capture_mode", "strict") == "strict"
    assert parse_setting("capture_assistant", "off") is False
    with pytest.raises(ValueError, match="smart, strict"):
        parse_setting("capture_mode", "everything")
    assert Config().capture_mode == "smart" and Config().capture_assistant is True


class FakeMem0:
    def __init__(self, results, existing=(), verdict="NO"):
        self.results, self.added, self.deleted = results, [], []
        self.existing, self.verdict, self.asked = list(existing), verdict, []
        self.llm = type("L", (), {"generate_response": lambda s, msgs, **k: (self.asked.append(msgs[0]["content"]), self.verdict)[1]})()

    def search(self, query, filters=None, top_k=5, threshold=0.0):
        return {"results": [dict(x) for x in self.existing]}

    def add(self, msgs, user_id=None, prompt=None, infer=True):
        self.added.append(msgs[0]["content"])
        return {"results": list(self.results)}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def run_worker(results, text, source="prompt", cwd=None, existing=(), verdict="NO"):
    fake = FakeMem0(results, existing, verdict)
    svc = MemoryService(Config(min_chars=5), factory=lambda c: fake)
    svc.start()
    assert svc.capture(text, "u", source, cwd) is None
    svc.drain()
    return svc, fake


def test_worker_deletes_narration_and_keeps_knowledge():
    results = [{"id": "k1", "memory": "The project Atlas runs unit tests with pnpm test:unit"},
               {"id": "j1", "memory": "The user ran the tests and identified 3 failed tests"},
               {"id": "k2", "memory": "The dev server of Atlas listens on port 4310"}]
    source = "My project Atlas runs unit tests with pnpm test:unit, its dev server listens on port 4310, and 3 failed today"
    svc, fake = run_worker(results, source)
    assert fake.deleted == ["j1"]
    assert svc.stats["memories_stored"] == 2 and svc.stats["narration_dropped"] == 1
    stored = [e for e in svc.events.recent() if e["kind"] == "stored"][0]
    assert stored["hits"] == 2 and "narration" not in stored["detail"] and "ran the tests" not in stored["detail"]


def test_replies_are_labelled_and_attributed_to_the_project_folder():
    svc, fake = run_worker([], "The backend is a FastAPI app in src/api.", source="turn", cwd="C:/work/atlas")
    assert fake.added == ["(Project folder: atlas)\nNotes from my coding assistant about my project:\n"
                          "The backend is a FastAPI app in src/api."]
    assert [e["source"] for e in svc.events.recent() if e["kind"] == "stored"] == ["turn"]
    _, fake2 = run_worker([], "Refactor the payment module to use async", source="prompt")
    assert fake2.added == ["Refactor the payment module to use async"]  # prompts are passed through as typed


def test_retry_only_when_facts_were_expected():
    _, task = run_worker([], "Refactor the payment module so it uses async functions")
    assert len(task.added) == 1          # a task legitimately yields nothing: no second LLM call
    _, statement = run_worker([], "My project Atlas runs its tests with pnpm test:unit")
    assert len(statement.added) == 2     # a clear statement that yielded nothing is retried once


def test_extractions_are_isolated_from_earlier_messages():
    """mem0 would otherwise feed the last 10 captured messages into every extraction."""
    from types import SimpleNamespace

    from recallhook.memory import isolate_extractions

    saved = []
    db = SimpleNamespace(get_last_messages=lambda scope, limit=10: ["old Atlas prompt"],
                         save_messages=lambda msgs, scope: saved.append(msgs))
    isolate_extractions(SimpleNamespace(db=db))
    assert db.get_last_messages("user_id=default", limit=10) == []   # no context from earlier captures
    db.save_messages(["raw prompt"], "user_id=default")
    assert saved == []                                              # and no raw copy of the text is kept
    isolate_extractions(SimpleNamespace())                          # tolerates an engine without a db


# -- grounding: real outputs from the experiment where a small model invented or copied facts ---------
from recallhook.inject import has_specifics, is_grounded  # noqa: E402

VAGUE_PROMPT = ("(Project folder: quartz-api)\nLook at this project and give me a 3 sentence overview: how to run "
                "the tests, what port it runs on, which database it uses and how it is deployed.")
REAL_REPLY = ("(Project folder: quartz-api)\nNotes from my coding assistant about my project:\nRun tests with "
              "`jest --runInBand`; the application runs on port 8123 locally and uses MySQL 8 as its database "
              "(configured via .env.local, never commit it). The API is deployed to Render using the render.yaml "
              "blueprint. The project uses yarn, never npm.")

COPIED_OR_INVENTED = [  # what the model produced from the vague prompt: none of it is in the source
    "The project quartz-api runs its unit tests using pnpm test:unit",                       # copied from another project
    "The dev server for quartz-api listens on port 4310",
    "The project quartz-api uses pnpm for package management",
    "The project quartz-api stores feature flags in config/flags.json",
    "The project quartz-api uses conventional commit messages in the imperative mood",
    "The project quartz-api deploys to Fly.io in the Singapore region using fly deploy --region sin",
    "The quartz-api project uses Java 17 and Spring Boot 3.1.5 for its backend services.",    # invented from nothing
    "To run the tests, execute 'mvn test' from the project root directory.",
    "The application runs on port 8080 by default.",
    "The project uses an embedded H2 database for development and a PostgreSQL database for production.",
    "The project is deployed using Docker containers with a Kubernetes cluster for orchestration.",
]
SUPPORTED = [  # what it produced from the real reply
    "The project quartz-api runs its unit tests using jest --runInBand",
    "The project quartz-api runs locally on port 8123",
    "The project quartz-api uses MySQL 8 as its database",
    "The project quartz-api uses .env.local for database configuration",
    "The project quartz-api is deployed to Render using the render.yaml blueprint",
    "The project quartz-api uses yarn as its package manager",
    "The application runs on port 8123 locally",
    "The project is deployed to Render using the render.yaml blueprint file",
]


@pytest.mark.parametrize("memory", COPIED_OR_INVENTED)
def test_ungrounded_memories_are_rejected(memory):
    assert not is_grounded(memory, VAGUE_PROMPT), memory


@pytest.mark.parametrize("memory", SUPPORTED)
def test_supported_memories_are_kept(memory):
    assert is_grounded(memory, REAL_REPLY), memory


def test_a_copied_fact_is_rejected_even_from_a_detailed_source():
    assert not is_grounded("The dev server listens on port 4310", REAL_REPLY)        # 4310 never appears


@pytest.mark.parametrize("text,expected", [
    ("The checkout service is in services/checkout", True), ("Edit package.json please", True),
    ("Run `jest --runInBand` first", True), ("The dev server listens on port 4310", True),
    ("We are on version 3.11 now", True), ("Set DATABASE_URL in the environment", True),
    ("Look at this project and give me a 3 sentence overview", False),
    ("Refactor the payment module so it uses async functions", False), ("Explain what a mutex is", False)])
def test_has_specifics(text, expected):
    assert has_specifics(text) is expected


def test_worker_drops_ungrounded_memories_even_without_narration_wording():
    results = [{"id": "ok", "memory": "The project quartz-api runs locally on port 8123"},
               {"id": "bad", "memory": "The dev server for quartz-api listens on port 4310"}]
    svc, fake = run_worker(results, REAL_REPLY, source="turn")
    assert fake.deleted == ["bad"] and svc.stats["memories_stored"] == 1


def test_list_all_is_not_capped_at_mems_default_of_20():
    seen = {}

    class Client:
        def get_all(self, filters=None, top_k=20):
            seen["top_k"] = top_k
            return {"results": [{"id": str(i), "memory": f"m{i}"} for i in range(min(top_k, 50))]}

    svc = MemoryService(Config(), factory=lambda c: Client())
    assert len(svc.list_all("u")) == 50 and seen["top_k"] >= 1000


# -- snapshots of work in progress go stale, so they are never stored -------------------------------------
# (anonymised look-alikes of memories a real user found stale in their own store)
SNAPSHOTS = [
    "The project media-archive has 46 files with no embedded date that may be incorrectly filed under `2024/2024-08/`",
    "The project media-archive has 16 files from 2000 that are filed under `2000/2000-07/` based on EXIF data",
    "The project media-archive has 831 adopted videos that are not yet consolidated into the backup process",
    "The project media-archive has 631 videos in the `Import_oct` folder that are not in `D:/Backup` and may need to be copied manually",
    "The build has 3 failing tests and 12 warnings",
    "The migration is pending and 5 records are unresolved",
]
HOW_IT_IS_SET_UP = [
    "The project media-archive uses a hash-checked, dated-folder process to organize files into `D:/Backup`",
    "The project media-archive includes a folder named `Undated/` for files with no embedded date that need review",
    "The project media-archive uses QuickTime tags to determine the date for videos",
    "The project media-archive organizes files into yearly folders such as `2000/`, `2021/` and `2022/`",
    "The project media-archive uses EXIF data, video internal dates, and phone file dates to determine the correct folder",
    "The project Atlas deploys to Fly.io in region fra and runs PostgreSQL 16 on port 5433",
    "The test suite uses 4 workers and runs with pytest -q",
]


@pytest.mark.parametrize("text", SNAPSHOTS)
def test_work_in_progress_snapshots_are_not_stored(text):
    assert is_ephemeral(text), text


@pytest.mark.parametrize("text", HOW_IT_IS_SET_UP)
def test_how_a_project_is_set_up_is_still_stored(text):
    assert not is_ephemeral(text), text


def test_old_memories_carry_their_age():
    from datetime import datetime, timedelta, timezone

    from recallhook.inject import age_label

    def ago(days):
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    assert age_label(ago(2)) is None                 # recent: no label needed
    assert age_label(ago(10)) == "1 week ago"
    assert age_label(ago(21)) == "3 weeks ago"
    assert age_label(ago(45)) == "1 month ago"
    assert age_label(ago(400)) == "13 months ago"
    assert age_label(None) is None and age_label("not a date") is None


@pytest.mark.parametrize("text,expected", [
    ("Forget that the staging DB is on port 5433", "the staging DB is on port 5433"),
    ("forget about the old deploy script", "the old deploy script"),
    ("Please forget: we use npm here", "we use npm here"),
    ("forgetting to run the tests is a bad habit", None),     # not an instruction
    ("Forget it", None),                                       # too short to name a memory
    ("How do I forget a password in the browser?", None)])
def test_forget_requests(text, expected):
    from recallhook.inject import forget_request

    assert forget_request(text) == expected


def test_a_fact_is_not_pinned_on_a_project_the_source_never_named():
    """Seen live: 'Remember the demo codename is blue-heron-7' came back as a fact about an existing project."""
    bare = "Remember that the scratch codename for the forget demo is blue-heron-7 in our test setup."
    pinned = "The project media-archive has a scratch codename of blue-heron-7 for the forget demo in the test setup"
    assert not is_grounded(pinned, bare)                                   # the source never mentions media-archive
    assert is_grounded(pinned, "(Project folder: media-archive)\n" + bare)  # but it did run in that folder
    assert is_grounded("The project uses hatchling for builds", "We build with hatchling")   # no name involved
    assert is_grounded("The project Atlas runs unit tests with pnpm test:unit", "My project Atlas runs unit tests with pnpm test:unit")
    assert not is_grounded("The project Atlas runs unit tests with pnpm test:unit", "We run unit tests with pnpm test:unit")



# -- replacing outdated memories ---------------------------------------------------------------------------
from recallhook.reconcile import same_slot, supersedes  # noqa: E402


@pytest.mark.parametrize("old,new,expected", [
    ("The dev server for atlas listens on port 4310", "The dev server for atlas listens on port 4311", True),
    ("Atlas requires Node 20", "Atlas now requires Node 22", False),          # two tokens differ ("now"): leave to the model
    ("The dev server listens on port 4310", "The api server listens on port 4311", False),   # different thing
    ("The test server listens on port 4311", "The dev server listens on port 4311", False),  # a word changed, not a number
    ("The project uses npm for package management", "The project uses pnpm for package management", False),  # not a number
    ("Same text here for the fact", "Same text here for the fact", False)])
def test_same_slot_only_for_a_single_changed_number(old, new, expected):
    assert same_slot(old, new) is expected


@pytest.mark.parametrize("raw,expected", [("YES", True), ("yes, because the port changed", True), (" Yes.", True),
                                          ("NO", False), ("No - different servers", False), ("maybe", False),
                                          ("", False), (None, False), ("YESTERDAY it moved", False)])
def test_only_a_clear_yes_replaces(raw, expected):
    assert supersedes(lambda msgs, **k: raw, "old fact", "new fact") is expected


def test_a_failing_model_never_deletes_anything():
    def boom(msgs, **k):
        raise RuntimeError("ollama is down")

    assert supersedes(boom, "old", "new") is False


NEW_FACT = [{"id": "n1", "memory": "The dev server for atlas listens on port 4311"}]
SOURCE = "We moved the dev server for atlas to port 4311"


def test_a_changed_number_retires_the_old_memory_without_asking_the_model():
    old = {"id": "o1", "memory": "The dev server for atlas listens on port 4310", "score": 0.9}
    svc, fake = run_worker(NEW_FACT, SOURCE, existing=[old])
    assert fake.deleted == ["o1"] and fake.asked == []          # deterministic: no model call
    assert svc.stats["outdated_replaced"] == 1
    ev = [e for e in svc.events.recent() if e["kind"] == "superseded"][0]
    import json
    assert json.loads(ev["detail"]) == {"old": old["memory"], "new": NEW_FACT[0]["memory"]}   # kept for the undo


def test_the_model_decides_when_more_than_a_number_changed():
    old = {"id": "o1", "memory": "The project atlas uses npm for package management", "score": 0.8}
    new = [{"id": "n1", "memory": "The project atlas uses pnpm for package management"}]
    _, yes = run_worker(new, "atlas uses pnpm for package management now", existing=[old], verdict="YES")
    assert yes.deleted == ["o1"] and len(yes.asked) == 1
    _, no = run_worker(new, "atlas uses pnpm for package management now", existing=[old], verdict="NO")
    assert no.deleted == []                                     # anything but a clear YES keeps the old memory


def test_replacement_has_safety_limits_and_a_switch():
    olds = [{"id": f"o{i}", "memory": f"The dev server for atlas listens on port 43{i}0", "score": 0.9} for i in range(5)]
    svc, fake = run_worker(NEW_FACT, SOURCE, existing=olds)
    assert len(fake.deleted) == 2                               # one new fact retires at most two memories
    cfg = Config(min_chars=5, replace_outdated=False)
    fake2 = FakeMem0(NEW_FACT, existing=olds)
    svc2 = MemoryService(cfg, factory=lambda c: fake2)
    svc2.start()
    svc2.capture(SOURCE, "u")
    svc2.drain()
    assert fake2.deleted == []                                  # `replace_outdated false` turns it off


def test_memories_from_the_same_capture_never_retire_each_other():
    batch = [{"id": "a", "memory": "The dev server for atlas listens on port 4310"},
             {"id": "b", "memory": "The dev server for atlas listens on port 4311"}]
    existing = [dict(batch[0], score=0.9)]                      # search finds the sibling that was just stored
    _, fake = run_worker(batch, "atlas dev server on port 4310 and 4311 depending on the mode", existing=existing)
    assert fake.deleted == []


# -- secrets never reach the model or a memory -------------------------------------------------------------
from recallhook.inject import looks_secret, redact_secrets  # noqa: E402

FAKE_SECRETS = [  # obviously fake values in the shapes real credentials take
    "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "sk-" + "proj-abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "xoxb-" + "123456789012-abcdefghijklmnop",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz123",
    "pypi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6MDEyMzQ1Njc4OQ",
]


@pytest.mark.parametrize("secret", FAKE_SECRETS)
def test_credentials_are_masked(secret):
    out = redact_secrets(f"the deploy key is {secret} and it works")
    assert secret not in out and "[redacted" in out and "deploy key is" in out


def test_assignments_and_urls_with_credentials_are_masked():
    assert redact_secrets("DATABASE_PASSWORD=hunter2hunter2") == "DATABASE_PASSWORD=[redacted]"
    assert "hunter2hunter2" not in redact_secrets('config = {"api_key": "hunter2hunter2"}')
    assert redact_secrets("postgres://admin:s3cr3tpass@db.internal:5432/app") == "postgres://admin:[redacted]@db.internal:5432/app"
    assert "abcdefghijklmnopqrstuvwxyz" not in redact_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA PRIVATE KEY-----"
    assert redact_secrets("key:\n" + pem) == "key:\n[redacted key]"


@pytest.mark.parametrize("text", [
    "The dev server listens on port 4310", "Tests run with pytest -q", "Commit abc123def4567890abc123def4567890abc12345",
    "The token bucket limiter allows 100 requests per minute", "Set the DATABASE_URL environment variable",
    "The project uses PostgreSQL 16 on port 5433", "Authentication uses OAuth with PKCE"])
def test_ordinary_project_text_is_left_alone(text):
    assert redact_secrets(text) == text and not looks_secret(text)


def test_a_secret_never_reaches_the_model_and_a_leaked_one_is_deleted():
    secret = FAKE_SECRETS[0]
    results = [{"id": "ok", "memory": "The deploy script reads the token from the environment"},
               {"id": "leak", "memory": f"The deploy token is {secret}"}]
    svc, fake = run_worker(results, f"The deploy script reads the token from the environment; for now it is {secret} here")
    assert all(secret not in sent for sent in fake.added)          # redacted before extraction
    assert fake.deleted == ["leak"] and svc.stats["memories_stored"] == 1   # and a memory containing one is dropped anyway
