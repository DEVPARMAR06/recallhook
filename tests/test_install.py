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
    ups, stop = d["hooks"]["UserPromptSubmit"], d["hooks"]["Stop"]
    assert len(ups) == 2 and ups[0] == other and len(stop) == 2 and stop[0] == other   # still exactly one of ours each
    assert "recallhook.cli hook" in ups[1]["hooks"][0]["command"]
    assert "recallhook.cli hook" in stop[1]["hooks"][0]["command"] and d["theme"] == "dark"
    install_hook(False, f)
    d = json.loads(f.read_text())
    assert d["hooks"]["UserPromptSubmit"] == [other] and d["hooks"]["Stop"] == [other]


def test_hook_prints_nothing_when_gateway_is_down(capsys, monkeypatch):
    import io
    import sys

    from recallhook import hook

    event = json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "hello there", "session_id": "s"})
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(event.encode())})())
    assert hook.run(port=9) == 0  # nothing listens on port 9
    assert capsys.readouterr().out == ""


def test_hook_routes_events_and_never_prints_for_stop(monkeypatch, capsys):
    import io
    import sys

    from recallhook import hook

    posts = []
    monkeypatch.setattr(hook, "_post", lambda port, path, body: posts.append((path, body)) or {"block": "BLOCK"})

    def fire(event: dict):
        monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(json.dumps(event).encode())})())
        return hook.run(port=1)

    assert fire({"hook_event_name": "UserPromptSubmit", "prompt": "hello there friend", "session_id": "s", "cwd": "/w/a"}) == 0
    assert posts[-1] == ("prompt", {"text": "hello there friend", "session": "s", "cwd": "/w/a"})
    assert "BLOCK" in capsys.readouterr().out                       # recall output goes to Claude

    assert fire({"hook_event_name": "Stop", "last_assistant_message": "The repo uses pytest.", "session_id": "s", "cwd": "/w/a"}) == 0
    assert posts[-1] == ("turn", {"text": "The repo uses pytest.", "session": "s", "cwd": "/w/a"})
    assert capsys.readouterr().out == ""                            # a Stop hook must stay silent

    n = len(posts)
    fire({"hook_event_name": "Stop", "last_assistant_message": "x", "stop_hook_active": True})   # no loops
    fire({"hook_event_name": "Stop", "last_assistant_message": ""})
    fire({"hook_event_name": "SessionStart"})
    assert len(posts) == n


def test_has_stop_hook_sees_our_hook_or_the_plugin(tmp_path):
    from recallhook.install import has_stop_hook, install_hook

    f = tmp_path / "settings.json"
    assert has_stop_hook(f) is False
    install_hook(True, f)
    assert has_stop_hook(f) is True
    install_hook(False, f)
    f.write_text(json.dumps({"enabledPlugins": {"recallhook@recallhook": True}}))
    assert has_stop_hook(f) is True
