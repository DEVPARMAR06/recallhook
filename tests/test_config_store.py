import json

import pytest

from recallhook import store
from recallhook.config import (Config, config_path, effective_port, parse_setting, read_file,
                               update_file)


# -- config precedence -----------------------------------------------------------
def test_file_overrides_default_and_env_overrides_file(monkeypatch):
    assert Config().llm_model == "qwen3:8b" and Config().top_k == 5
    update_file("llm_model", "llama3.2:3b")
    update_file("top_k", 9)
    c = Config()
    assert c.llm_model == "llama3.2:3b" and c.top_k == 9
    monkeypatch.setenv("RECALLHOOK_LLM_MODEL", "from-env")
    assert Config().llm_model == "from-env" and Config().top_k == 9


def test_bad_values_in_the_file_fall_back_to_defaults():
    config_path().parent.mkdir(parents=True, exist_ok=True)
    config_path().write_text(json.dumps({"top_k": "many", "min_score": 0.7, "inject": False}))
    c = Config()
    assert c.top_k == 5 and c.min_score == 0.7 and c.inject is False
    config_path().write_text("{not json")
    assert Config().top_k == 5  # corrupt file never breaks startup


def test_update_file_set_and_unset_roundtrip():
    update_file("port", 9000)
    assert read_file() == {"port": 9000}
    update_file("port", None)
    assert read_file() == {}


def test_effective_port_env_then_file_then_default(monkeypatch):
    assert effective_port() == 8788
    update_file("port", 9001)
    assert effective_port() == 9001
    monkeypatch.setenv("RECALLHOOK_PORT", "9002")
    assert effective_port() == 9002


@pytest.mark.parametrize("key,text,expected", [
    ("llm_model", "qwen3:4b", "qwen3:4b"), ("top_k", "3", 3), ("min_score", "0.65", 0.65),
    ("inject", "off", False), ("write", "yes", True)])
def test_parse_setting_accepts_valid(key, text, expected):
    assert parse_setting(key, text) == expected


@pytest.mark.parametrize("key,text", [
    ("nonsense", "1"), ("top_k", "abc"), ("min_score", "1.5"), ("port", "0"), ("inject", "maybe")])
def test_parse_setting_rejects_invalid(key, text):
    with pytest.raises(ValueError):
        parse_setting(key, text)


# -- embedding guard + reindex -----------------------------------------------------
def test_store_remembers_its_embedder_and_refuses_a_silent_change():
    cfg = Config()
    store.check(cfg)  # first run records nomic/768
    assert store.read_meta(cfg) == {"embed_model": "nomic-embed-text:latest", "embed_dims": 768}
    cfg.embed_model, cfg.embed_dims = "mxbai-embed-large", 1024
    with pytest.raises(store.StoreMismatch) as e:
        store.check(cfg)
    assert "recallhook reindex" in str(e.value) and "nomic-embed-text:latest" in str(e.value)


def test_check_with_write_false_does_not_create_metadata():
    cfg = Config()
    store.check(cfg, write=False)
    assert store.read_meta(cfg) is None


class FakeClient:
    def close(self):
        pass


class FakeMem:
    def __init__(self, points=()):
        self.points, self.added = list(points), []
        self.vector_store = type("VS", (), {"list": lambda s, f, n: (self.points, None), "client": FakeClient()})()

    def add(self, msgs, user_id=None, infer=True):
        self.added.append((msgs[0]["content"], user_id, infer))


class Point:
    def __init__(self, data, user):
        self.payload = {"data": data, "user_id": user}


def test_reindex_reembeds_every_memory_without_reextracting(tmp_path):
    cfg = Config()
    store.check(cfg)  # store built with the default embedder
    (cfg.home / "qdrant").mkdir(parents=True)
    (cfg.home / "qdrant" / "data.bin").write_text("old vectors")
    cfg.embed_model, cfg.embed_dims = "mxbai-embed-large", 1024
    old = FakeMem([Point("User uses pnpm", "u1"), Point("Atlas deploys to Fly.io", "u2")])
    new = FakeMem()
    built = []

    def builder(c, check=True):
        built.append((c.embed_model, c.embed_dims, check))
        return old if len(built) == 1 else new

    assert store.reindex(cfg, builder) == 2
    assert built == [("nomic-embed-text:latest", 768, False), ("mxbai-embed-large", 1024, False)]
    assert new.added == [("User uses pnpm", "u1", False), ("Atlas deploys to Fly.io", "u2", False)]
    assert list(cfg.home.glob("qdrant.bak-*"))  # old vectors kept as a backup
    assert store.read_meta(cfg)["embed_model"] == "mxbai-embed-large"
    store.check(cfg)  # now consistent


def test_reindex_is_a_noop_when_nothing_changed():
    cfg = Config()
    store.check(cfg)
    assert store.reindex(cfg, lambda *a, **k: pytest.fail("must not build anything")) == 0


def test_build_mem0_runs_the_store_guard_and_tunes_ollama(monkeypatch):
    """Regression: a local variable once shadowed the `store` module inside build_mem0."""
    from types import SimpleNamespace

    import mem0

    from recallhook.memory import build_mem0

    fake = SimpleNamespace(llm=SimpleNamespace(client=SimpleNamespace(chat=lambda **k: None)),
                           embedding_model=SimpleNamespace(client=SimpleNamespace(embed=lambda **k: None)))
    seen = {}
    monkeypatch.setattr(mem0.Memory, "from_config", classmethod(lambda cls, conf: seen.update(conf=conf) or fake))
    cfg = Config()
    assert build_mem0(cfg) is fake
    assert store.read_meta(cfg)["embed_model"] == "nomic-embed-text:latest"  # guard ran and recorded it
    assert seen["conf"]["vector_store"]["config"]["embedding_model_dims"] == 768
    cfg.embed_model = "other"
    with pytest.raises(store.StoreMismatch):
        build_mem0(cfg)
    assert build_mem0(cfg, check=False) is fake
