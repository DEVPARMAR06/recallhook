import json

from recallhook.install import route_claude


def test_route_on_off_touches_only_our_key(tmp_path):
    f = tmp_path / "settings.json"
    original = {"model": "sonnet", "env": {"FOO": "1"}, "theme": "dark"}
    f.write_text(json.dumps(original))
    route_claude(True, 8788, f)
    on = json.loads(f.read_text())
    assert on["env"] == {"FOO": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:8788"}
    assert {k: v for k, v in on.items() if k != "env"} == {"model": "sonnet", "theme": "dark"}
    assert (tmp_path / "settings.json.recallhook.bak").exists()
    route_claude(False, 8788, f)
    assert json.loads(f.read_text()) == original


def test_route_off_leaves_foreign_value_alone(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://my.proxy"}}))
    assert "left as is" in route_claude(False, 8788, f)
    assert json.loads(f.read_text())["env"]["ANTHROPIC_BASE_URL"] == "https://my.proxy"


def test_hook_install_is_idempotent_and_preserves_other_hooks(tmp_path):
    from recallhook.install import install_hook

    f = tmp_path / "settings.json"
    other = {"hooks": [{"type": "command", "command": "echo hi"}]}
    f.write_text(json.dumps({"theme": "dark", "hooks": {"UserPromptSubmit": [other], "Stop": [other]}}))
    install_hook(True, f)
    install_hook(True, f)  # twice: still one entry of ours
    d = json.loads(f.read_text())
    ups = d["hooks"]["UserPromptSubmit"]
    assert len(ups) == 2 and ups[0] == other
    assert "recallhook.cli hook" in ups[1]["hooks"][0]["command"]
    assert d["hooks"]["Stop"] == [other] and d["theme"] == "dark"
    install_hook(False, f)
    d = json.loads(f.read_text())
    assert d["hooks"]["UserPromptSubmit"] == [other]


def test_hook_prints_nothing_when_gateway_is_down(capsys, monkeypatch):
    import io
    import sys

    from recallhook import hook

    event = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello there", "session_id": "s"})
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(event.encode())})())
    assert hook.run(port=9) == 0  # nothing listens on port 9
    assert capsys.readouterr().out == ""
