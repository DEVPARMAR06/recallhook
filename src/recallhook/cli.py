"""recallhook serve | doctor | install | uninstall | hooks | route | status | ui"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request

from .config import Config


def _parser() -> argparse.ArgumentParser:
    d = Config()
    p = argparse.ArgumentParser("recallhook", description="mem0 memory in front of the Headroom proxy")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "doctor"):
        s = sub.add_parser(name)
        s.add_argument("--upstream", default=d.upstream, help="Headroom proxy URL")
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
        if name == "serve":
            s.add_argument("--spawn-headroom", action="store_true",
                           help="also run (and keep alive) `headroom proxy` on the upstream port")
    i = sub.add_parser("install", help="start at logon (Windows) + add the Claude Code memory hook")
    i.add_argument("--route-claude", action="store_true",
                   help="also route Claude Code traffic through the gateway (not recommended: no measured saving)")
    i.add_argument("--with-headroom", action="store_true", help="also run the Headroom proxy (opt-in)")
    h = sub.add_parser("hooks", help="add or remove the Claude Code memory hook")
    h.add_argument("state", choices=["on", "off"])
    sub.add_parser("hook", help="(internal) the UserPromptSubmit hook; reads the event from stdin")
    sub.add_parser("uninstall", help="remove autostart + Claude routing, stop the processes")
    r = sub.add_parser("route", help="turn Claude Code routing on or off")
    r.add_argument("state", choices=["on", "off"])
    sub.add_parser("status")
    sub.add_parser("ui", help="open the memory dashboard in your browser")
    return p


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def doctor(cfg: Config) -> int:
    ok = True

    def check(label: str, fn) -> None:
        nonlocal ok
        try:
            print(f"[ok]   {label}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {label}: {type(e).__name__}: {e}")

    def models() -> str:
        have = {m["name"] for m in _get(cfg.ollama_url + "/api/tags")["models"]}
        missing = [m for m in (cfg.llm_model, cfg.embed_model) if m not in have]
        if missing:
            raise RuntimeError(f"not pulled: {missing} (run `ollama pull <name>`)")
        return f"{cfg.llm_model}, {cfg.embed_model}"

    check("ollama models", models)
    try:  # the Headroom proxy is optional, so its absence is not a failure
        code = urllib.request.urlopen(cfg.upstream.rstrip("/") + "/livez", timeout=3).status
        print(f"[ok]   headroom proxy (optional): {cfg.upstream} -> HTTP {code}")
    except Exception:  # noqa: BLE001
        print("[--]   headroom proxy (optional): not running")
    check("gateway", lambda: _get(f"http://127.0.0.1:{cfg.port}/_recallhook/health")["stats"])
    return 0 if ok else 1


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
        if hasattr(a, k):
            setattr(cfg, k, getattr(a, k))
    from . import install as ins

    if a.cmd in ("serve", "doctor"):
        return doctor(cfg) if a.cmd == "doctor" else serve(cfg, a.spawn_headroom)
    if a.cmd == "hook":
        from .hook import run

        return run(cfg.port)
    if a.cmd == "hooks":
        print(ins.install_hook(a.state == "on"))
        return 0
    if a.cmd == "install":
        print(ins.install_task("serve --spawn-headroom" if a.with_headroom else "serve"))
        print(ins.install_hook(True))
        if a.route_claude:
            print(ins.route_claude(True, cfg.port))
        return 0
    if a.cmd == "route":
        print(ins.route_claude(a.state == "on", cfg.port))
        return 0
    if a.cmd == "uninstall":
        print(ins.route_claude(False, cfg.port))
        print(ins.install_hook(False))
        print(ins.remove_task())
        print("stopped pids:", ins.kill_ports(cfg.port, int(cfg.upstream.rsplit(":", 1)[-1])) or "none")
        return 0
    if a.cmd == "status":
        return doctor(cfg)
    if a.cmd == "ui":
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{cfg.port}/_recallhook/ui")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
