import sys

import pytest

from recallhook import cli, diagnostics, install as ins
from recallhook.config import Config, read_file


def run(*argv):
    return cli.main(list(argv))


# -- config command ----------------------------------------------------------------
def test_config_set_saves_and_validates(capsys):
    assert run("config", "set", "llm_model", "llama3.2:3b", "--no-restart") == 0
    assert read_file()["llm_model"] == "llama3.2:3b"
    assert run("config", "set", "min_score", "7", "--no-restart") == 2
    assert "between" in capsys.readouterr().out
    assert read_file()["llm_model"] == "llama3.2:3b" and "min_score" not in read_file()


def test_config_set_embed_model_points_to_reindex(capsys):
    run("config", "set", "embed_model", "mxbai-embed-large", "--no-restart")
    assert "reindex" in capsys.readouterr().out


def test_config_show_marks_where_each_value_comes_from(capsys, monkeypatch):
    run("config", "set", "top_k", "3", "--no-restart")
    monkeypatch.setenv("RECALLHOOK_LLM_MODEL", "x")
    capsys.readouterr()
    run("config", "show")
    out = capsys.readouterr().out
    assert "top_k" in out and "[file]" in out and "[env]" in out and "[default]" in out


def test_config_unset_restores_default():
    run("config", "set", "top_k", "3", "--no-restart")
    run("config", "unset", "top_k")
    assert "top_k" not in read_file()


# -- init ---------------------------------------------------------------------------
@pytest.fixture
def fake_system(monkeypatch):
    calls = {"pulled": [], "installed": []}
    monkeypatch.setattr(ins, "kill_ports", lambda *p: [])
    monkeypatch.setattr(ins, "install_task", lambda args="serve": calls["installed"].append("task") or "task ok")
    monkeypatch.setattr(ins, "install_hook", lambda on, settings=None: calls["installed"].append("hook") or "hook ok")
    monkeypatch.setattr(cli, "_gateway_up", lambda cfg: True)
    monkeypatch.setattr(diagnostics, "pull_model", lambda cfg, m, say=print: calls["pulled"].append(m))
    return calls


def test_init_downloads_missing_models_then_installs(fake_system, monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "installed_models", lambda cfg: {"nomic-embed-text:latest"})
    assert run("init", "--yes") == 0
    assert fake_system["pulled"] == ["qwen3:8b"]  # only the missing one
    assert fake_system["installed"] == ["task", "hook"]
    assert "Dashboard" in capsys.readouterr().out


def test_init_saves_the_chosen_model(fake_system, monkeypatch):
    monkeypatch.setattr(diagnostics, "installed_models", lambda cfg: {"nomic-embed-text:latest", "llama3.2:3b"})
    assert run("init", "--llm-model", "llama3.2:3b", "--yes") == 0
    assert read_file()["llm_model"] == "llama3.2:3b" and fake_system["pulled"] == []


def test_init_explains_when_ollama_is_missing(fake_system, monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "installed_models", lambda cfg: None)
    assert run("init", "--yes") == 1
    assert "ollama.com" in capsys.readouterr().out and fake_system["installed"] == []


def test_init_never_downloads_without_consent(fake_system, monkeypatch):
    monkeypatch.setattr(diagnostics, "installed_models", lambda cfg: set())
    assert run("init") == 1  # no --yes and stdin is not a terminal under pytest
    assert fake_system["pulled"] == [] and fake_system["installed"] == []


def test_init_embed_model_needs_dims(fake_system, monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "installed_models", lambda cfg: set())
    assert run("init", "--embed-model", "mxbai-embed-large", "--yes") == 1
    assert "--embed-dims" in capsys.readouterr().out


# -- status / diagnostics --------------------------------------------------------------
def test_status_reports_each_problem_with_its_fix(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "check", lambda cfg: ["Model 'qwen3:8b' is not downloaded. Run: ollama pull qwen3:8b"])
    monkeypatch.setattr(ins, "hook_state", lambda *a: ("missing", "run `recallhook install`"))
    monkeypatch.setattr(cli, "_gateway_up", lambda cfg: False)
    assert run("status") == 1
    out = capsys.readouterr().out
    assert "ollama pull qwen3:8b" in out and "hook missing" in out and "not running" in out


def test_status_is_clean_when_everything_works(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "check", lambda cfg: [])
    monkeypatch.setattr(ins, "hook_state", lambda *a: ("ok", "cmd"))
    monkeypatch.setattr(cli, "_gateway_up", lambda cfg: True)
    monkeypatch.setattr(cli, "_get", lambda url, timeout=5: {"memories": 3, "recalls": 9, "hit_rate": 50})
    assert run("status") == 0


def test_diagnostics_names_missing_models_and_unreachable_ollama(monkeypatch):
    cfg = Config()
    monkeypatch.setattr(diagnostics, "installed_models", lambda c, timeout=3.0: None)
    assert "not reachable" in diagnostics.check(cfg)[0]
    monkeypatch.setattr(diagnostics, "installed_models", lambda c, timeout=3.0: {"nomic-embed-text:latest"})
    assert diagnostics.check(cfg) == ["Model 'qwen3:8b' is not downloaded. Run: ollama pull qwen3:8b"]
    monkeypatch.setattr(diagnostics, "installed_models", lambda c, timeout=3.0: {"nomic-embed-text:latest", "qwen3:8b"})
    assert diagnostics.check(cfg) == []
    assert diagnostics.normalize("nomic-embed-text") == "nomic-embed-text:latest"


def test_hook_state_detects_a_moved_python(tmp_path):
    f = tmp_path / "settings.json"
    ins.install_hook(True, f)
    assert ins.hook_state(f)[0] == "ok"
    f.write_text(f.read_text().replace(sys.executable.replace("\\", "/"), "C:/gone/python.exe"))
    state, detail = ins.hook_state(f)
    assert state == "broken" and "recallhook install" in detail
    assert ins.hook_state(tmp_path / "none.json")[0] == "missing"


def test_memory_service_surfaces_why_it_cannot_start(monkeypatch):
    from recallhook import memory, store

    monkeypatch.setattr(diagnostics, "check", lambda cfg: [])

    def boom(cfg):
        raise store.StoreMismatch("embedder changed; run recallhook reindex")

    svc = memory.MemoryService(Config(), factory=boom)
    with pytest.raises(store.StoreMismatch):
        svc.client()
    assert svc.problems() == ["embedder changed; run recallhook reindex"]


# -- plugin awareness ------------------------------------------------------------------
def test_hook_state_recognises_the_plugin_and_flags_duplicates(tmp_path):
    import json

    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"enabledPlugins": {"recallhook@recallhook": True}}))
    assert ins.hook_state(f) == ("ok", "provided by the recallhook plugin")  # plugin only
    ins.install_hook(True, f)  # settings hook as well
    state, detail = ins.hook_state(f)
    assert state == "duplicate" and "recallhook hooks off" in detail
    ins.install_hook(False, f)
    f.write_text(json.dumps({"enabledPlugins": {"recallhook@recallhook": False}}))  # disabled plugin counts for nothing
    assert ins.hook_state(f)[0] == "missing"


def test_status_treats_duplicate_hook_as_a_warning_not_a_failure(monkeypatch, capsys):
    monkeypatch.setattr(diagnostics, "check", lambda cfg: [])
    monkeypatch.setattr(ins, "hook_state", lambda *a: ("duplicate", "both active"))
    monkeypatch.setattr(cli, "_gateway_up", lambda cfg: True)
    monkeypatch.setattr(cli, "_get", lambda url, timeout=5: {"memories": 1, "recalls": 1, "hit_rate": 1})
    assert run("status") == 0
    assert "both active" in capsys.readouterr().out


def test_install_no_hook_leaves_the_hook_to_the_plugin(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(ins, "kill_ports", lambda *p: [])
    monkeypatch.setattr(ins, "install_task", lambda args="serve": "task ok")
    monkeypatch.setattr(ins, "install_hook", lambda on, settings=None: called.append(on) or "hook ok")
    monkeypatch.setattr(diagnostics, "check", lambda cfg: [])
    assert run("install", "--no-hook") == 0
    assert called == [] and "plugin provides it" in capsys.readouterr().out
    assert run("install") == 0 and called == [True]
