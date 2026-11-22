"""recallhook: init | status | ui | config | install | uninstall | restart | reindex | serve | hooks | route"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request

from . import diagnostics
from .config import SETTABLE, Config, parse_setting, read_file, update_file, config_path


REPLY_NOTICE = ("recallhook learns project details from your prompts AND from Claude's final reply each turn "
                "(text only, capped at 6,000 characters; never tool output or file contents). It all stays on this "
                "machine, and obvious secrets (API keys, tokens, passwords) are masked before anything is stored. "
                "Turn reply learning off any time: recallhook config set capture_assistant false")


def _parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser(
        "recallhook", description="Private, local long-term memory for Claude Code (mem0 + Ollama).")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the memory gateway in the foreground")
    s.add_argument("--upstream", default=d.upstream, help="Headroom proxy URL (optional proxy mode)")
    s.add_argument("--fallback", default=d.fallback,
                   help="where to send requests if Headroom refuses connections ('' disables)")
    s.add_argument("--host", default=d.host)
    s.add_argument("--port", type=int, default=d.port)
    s.add_argument("--ollama-url", default=d.ollama_url)
    s.add_argument("--llm-model", default=d.llm_model, help="extraction model")
    s.add_argument("--embed-model", default=d.embed_model)
    s.add_argument("--num-ctx", type=int, default=d.num_ctx)
    s.add_argument("--store", dest="store_path", default=d.store_path)
    s.add_argument("--user-id", default=d.user_id)
    s.add_argument("--top-k", type=int, default=d.top_k)
    s.add_argument("--min-score", type=float, default=d.min_score)
    s.add_argument("--no-write", dest="write", action="store_false", default=d.write,
                   help="recall only; never store anything")
    s.add_argument("--no-inject", dest="inject", action="store_false", default=d.inject,
                   help="store only; never inject memories")
    s.add_argument("--spawn-headroom", action="store_true",
                   help="also run (and keep alive) `headroom proxy` on the upstream port")

    n = sub.add_parser("init", help="guided first-run setup: checks Ollama, downloads models, installs")
    n.add_argument("--llm-model", help="extraction model to use (saved to the config file)")
    n.add_argument("--embed-model", help="embedding model (also pass --embed-dims)")
    n.add_argument("--embed-dims", type=int, help="vector size of the embedding model")
    n.add_argument("--yes", "-y", action="store_true", help="download missing models without asking")
    n.add_argument("--no-service", action="store_true", help="only check/download; do not install autostart")
    n.add_argument("--no-hook", action="store_true",
                   help="install the service but not the settings hook (use with the Claude Code plugin)")
    n.add_argument("--no-assistant", action="store_true",
                   help="do not learn from Claude's replies, only from your prompts")

    i = sub.add_parser("install", help="start at logon (Windows) + add the Claude Code memory hook; also upgrades")
    i.add_argument("--route-claude", action="store_true",
                   help="also route Claude Code traffic through the gateway (not recommended: no measured saving)")
    i.add_argument("--with-headroom", action="store_true", help="also run the Headroom proxy (opt-in)")
    i.add_argument("--no-hook", action="store_true",
                   help="install the service but not the settings hook (use with the Claude Code plugin)")

    c = sub.add_parser("config", help="show or change settings (models, port, thresholds)")
    csub = c.add_subparsers(dest="action", required=True)
    csub.add_parser("show")
    csub.add_parser("path")
    cs = csub.add_parser("set")
    cs.add_argument("key", choices=sorted(SETTABLE))
    cs.add_argument("value")
    cs.add_argument("--no-restart", action="store_true", help="do not restart the running service")
    cu = csub.add_parser("unset")
    cu.add_argument("key", choices=sorted(SETTABLE))

    sub.add_parser("restart", help="restart the background service (loads new code/config)")
    ri = sub.add_parser("reindex", help="rebuild the store after changing the embedding model (keeps memories)")
    ri.add_argument("--yes", "-y", action="store_true")
    h = sub.add_parser("hooks", help="add or remove the Claude Code memory hook")
    h.add_argument("state", choices=["on", "off"])
    sub.add_parser("hook", help="(internal) the UserPromptSubmit hook; reads the event from stdin")
    sub.add_parser("uninstall", help="remove autostart, hook and routing; stop the processes")
    r = sub.add_parser("route", help="turn Claude Code proxy routing on or off (not recommended)")
    r.add_argument("state", choices=["on", "off"])
    sub.add_parser("status", help="what works, what doesn't, and how to fix it")
    sub.add_parser("doctor", help="alias of status")
    sub.add_parser("ui", help="open the memory dashboard in your browser")
    return p


def _get(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def _gateway_up(cfg: Config) -> bool:
    try:
        return bool(_get(f"http://127.0.0.1:{cfg.port}/_recallhook/health", 3).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print(f"{question} -> skipped (not interactive; re-run with --yes)")
        return False
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


# -- status -----------------------------------------------------------------------
def status(cfg: Config) -> int:
    from . import install as ins

    bad = 0

    def line(ok: bool | None, text: str) -> None:
        nonlocal bad
        bad += ok is False
        print(f"{'[ok]  ' if ok else '[FAIL]' if ok is False else '[--]  '} {text}")

    print(f"models: {cfg.llm_model} (extraction), {cfg.embed_model} (embeddings) | port {cfg.port}")
    problems = diagnostics.check(cfg)
    if problems:
        for p in problems:
            line(False, p)
    else:
        line(True, "Ollama is running and both models are downloaded; embedding model matches the store")
    state, detail = ins.hook_state()
    if state == "duplicate":
        line(None, f"Claude Code hook: {detail}")
    elif state == "ok":
        line(True, "Claude Code hook: " + ("provided by the recallhook plugin" if "plugin" in detail else "installed"))
    else:
        line(False, f"Claude Code hook {state}: {detail}")
    if cfg.capture_assistant and state in ("ok", "duplicate") and not ins.has_stop_hook():
        line(None, "Claude's replies are not being learned from yet: run `recallhook install` "
                   "(adds the Stop hook; reload your Claude session afterwards)")
    if _gateway_up(cfg):
        s = _get(f"http://127.0.0.1:{cfg.port}/_recallhook/api/summary")
        line(True, f"gateway running: {s.get('memories')} memories, {s.get('recalls')} recalls, "
                   f"hit rate {s.get('hit_rate')}%  (dashboard: http://127.0.0.1:{cfg.port}/_recallhook/ui)")
    else:
        line(False, "gateway is not running. Start it with `recallhook restart` (Windows autostart) "
                    "or `recallhook serve`")
    try:
        code = urllib.request.urlopen(cfg.upstream.rstrip("/") + "/livez", timeout=2).status
        line(None, f"headroom proxy (optional): running, HTTP {code}")
    except Exception:  # noqa: BLE001
        line(None, "headroom proxy (optional): not running")
    return 1 if bad else 0


# -- init -------------------------------------------------------------------------
def init(cfg: Config, a) -> int:
    from . import install as ins

    if a.llm_model:
        update_file("llm_model", a.llm_model)
        cfg.llm_model = a.llm_model
    if a.embed_model:
        if not a.embed_dims:
            print("--embed-model also needs --embed-dims (the vector size of that model).")
            return 1
        update_file("embed_model", a.embed_model)
        update_file("embed_dims", a.embed_dims)
        cfg.embed_model, cfg.embed_dims = a.embed_model, a.embed_dims
    print("1/3 Checking Ollama...")
    names = diagnostics.installed_models(cfg)
    if names is None:
        print(f"    Ollama is not reachable at {cfg.ollama_url}.\n"
              f"    Install it from https://ollama.com, start it, then run `recallhook init` again.")
        return 1
    have = {diagnostics.normalize(n) for n in names}
    missing = [m for m in dict.fromkeys((cfg.llm_model, cfg.embed_model)) if diagnostics.normalize(m) not in have]
    print("    Ollama is running." + (f" Missing models: {', '.join(missing)}" if missing else " Models are present."))
    print("2/3 Models...")
    for m in missing:
        if not _confirm(f"    Download '{m}' now? (can be several GB)", a.yes):
            print(f"    Not downloaded. Run `ollama pull {m}` yourself, then `recallhook init`.")
            return 1
        try:
            diagnostics.pull_model(cfg, m, say=lambda t: print("   " + t))
        except Exception as e:  # noqa: BLE001
            print(f"    Download of '{m}' failed ({type(e).__name__}: {e}). Try `ollama pull {m}`.")
            return 1
    if a.no_service:
        print("3/3 Skipped (--no-service). Run `recallhook install` when ready.")
        return 0
    print("Privacy: " + REPLY_NOTICE)
    if a.no_assistant:
        update_file("capture_assistant", False)
        cfg.capture_assistant = False
    elif cfg.capture_assistant and not a.yes and sys.stdin and sys.stdin.isatty():
        if input("Learn from Claude's replies too (recommended)? [Y/n] ").strip().lower() in ("n", "no"):
            update_file("capture_assistant", False)
            cfg.capture_assistant = False
    print("   replies: " + ("learned from" if cfg.capture_assistant else "NOT learned from (prompts only)"))
    print("3/3 Installing autostart + Claude Code hook...")
    ins.kill_ports(cfg.port)
    print("   ", ins.install_task("serve"))
    print("   ", "hook skipped (--no-hook): the recallhook plugin provides it" if a.no_hook else ins.install_hook(True))
    if sys.platform == "win32":
        for _ in range(60):
            if _gateway_up(cfg):
                break
            time.sleep(3)
        print("    gateway is up." if _gateway_up(cfg) else "    gateway is still starting; check `recallhook status`.")
    else:
        print("    Start the gateway with `recallhook serve` (autostart is Windows-only for now).")
    print(f"\nDone. Dashboard: http://127.0.0.1:{cfg.port}/_recallhook/ui\n"
          f"Open a NEW Claude Code session so the hook loads, tell it something lasting "
          f"(e.g. \"My project X uses pnpm\"), then ask about it in another new session.")
    return 0


# -- config -----------------------------------------------------------------------
def config_cmd(cfg: Config, a) -> int:
    from . import install as ins

    if a.action == "path":
        print(config_path())
        return 0
    if a.action == "show":
        stored = read_file()
        print(f"config file: {config_path()}" + ("" if config_path().exists() else "  (not created yet)"))
        for key in sorted(SETTABLE):
            src = "env" if os.environ.get(f"RECALLHOOK_{key.upper()}") is not None else (
                "file" if key in stored else "default")
            print(f"  {key:15s} {str(getattr(cfg, key)):28s} [{src}]")
        return 0
    if a.action == "unset":
        update_file(a.key, None)
        print(f"{a.key} reset to its default")
        return 0
    try:
        value = parse_setting(a.key, a.value)
    except ValueError as e:
        print(f"error: {e}")
        return 2
    update_file(a.key, value)
    print(f"{a.key} = {value}  (saved to {config_path()})")
    if a.key in ("embed_model", "embed_dims"):
        print("The embedding model changed: run `recallhook reindex` to rebuild the store (memories are kept).\n"
              "Similarity scores differ between models, so you may need to retune: `recallhook config set min_score <0-1>` "
              "(default 0.5 suits nomic-embed-text).")
    if a.key == "llm_model":
        print("Make sure it is downloaded: `recallhook init` (or `ollama pull " + str(value) + "`).")
    if not a.no_restart and _gateway_up(cfg):
        print(ins.restart_service(cfg.port))
    return 0


# -- reindex ----------------------------------------------------------------------
def reindex_cmd(cfg: Config, yes: bool) -> int:
    from . import install as ins, store
    from .memory import build_mem0

    meta = store.read_meta(cfg)
    if meta is None or (meta.get("embed_model"), meta.get("embed_dims")) == (cfg.embed_model, cfg.embed_dims):
        print("Nothing to do: the store already matches the configured embedding model.")
        return 0
    missing = [p for p in diagnostics.check(cfg) if "not downloaded" in p or "not reachable" in p]
    if missing:
        print("\n".join(missing))
        return 1
    if not _confirm(f"Rebuild the store from '{meta['embed_model']}' to '{cfg.embed_model}'? "
                    f"The service pauses briefly and the old vectors are backed up.", yes):
        return 1
    ins.pause_service(cfg.port)
    try:
        kept = store.reindex(cfg, build_mem0)
    finally:
        ins.resume_service(cfg.port)
    print(f"Done: {kept} memories re-embedded. The old vectors are in {cfg.home}\\qdrant.bak-*.")
    return 0


# -- serve ------------------------------------------------------------------------
def _setup_logging(cfg: Config) -> None:
    """pythonw has no stdout/stderr, so always log to a file there."""
    fmt = "%(asctime)s %(levelname)s %(message)s"
    if sys.stdout is None or sys.stderr is None:
        logdir = cfg.home / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        path = logdir / "gateway.log"
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(logdir / "gateway.old.log")
        sys.stdout = sys.stderr = open(path, "a", buffering=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format=fmt, stream=sys.stderr)


def serve(cfg: Config, spawn: bool) -> int:
    import uvicorn

    from .gateway import create_app

    _setup_logging(cfg)
    if spawn:
        from .supervisor import Supervisor, healthy

        sup = Supervisor(cfg.upstream, cfg.home / "logs" / "headroom.log")
        if not healthy(cfg.upstream):
            sup.spawn()
        sup.start()
    # One process / one worker: local Qdrant takes a file lock on the store.
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, workers=1, log_level="info",
                log_config=None)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    cfg = Config()
    for k in vars(cfg):
        if getattr(a, k, None) is not None:  # flags left unset must not wipe config-file/env values
            setattr(cfg, k, getattr(a, k))
    from . import install as ins

    if a.cmd == "serve":
        return serve(cfg, a.spawn_headroom)
    if a.cmd == "hook":
        from .hook import run

        return run(cfg.port)
    if a.cmd in ("status", "doctor"):
        return status(cfg)
    if a.cmd == "init":
        return init(cfg, a)
    if a.cmd == "config":
        return config_cmd(cfg, a)
    if a.cmd == "reindex":
        return reindex_cmd(cfg, a.yes)
    if a.cmd == "restart":
        print(ins.restart_service(cfg.port))
        return 0
    if a.cmd == "hooks":
        print(ins.install_hook(a.state == "on"))
        return 0
    if a.cmd == "install":
        ins.kill_ports(cfg.port)  # stop an old gateway so upgraded code loads
        print(ins.install_task("serve --spawn-headroom" if a.with_headroom else "serve"))
        print("hook skipped (--no-hook): the recallhook plugin provides it" if a.no_hook else ins.install_hook(True))
        if a.route_claude:
            print(ins.route_claude(True, cfg.port))
        for problem in diagnostics.check(cfg):
            print(f"Heads up: {problem}")
        return 0
    if a.cmd == "route":
        print(ins.route_claude(a.state == "on", cfg.port))
        return 0
    if a.cmd == "uninstall":
        print(ins.route_claude(False, cfg.port))
        print(ins.install_hook(False))
        print(ins.remove_task())
        print("stopped pids:", ins.kill_ports(cfg.port, int(cfg.upstream.rsplit(":", 1)[-1])) or "none")
        print("Your memories are kept in", cfg.home, "- delete that folder to erase them. "
              "You can now `pip uninstall recallhook`.")
        return 0
    if a.cmd == "ui":
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{cfg.port}/_recallhook/ui")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
