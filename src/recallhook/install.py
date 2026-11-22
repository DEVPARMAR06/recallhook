"""Autostart (Windows Task Scheduler) and Claude Code routing. Reversible: see uninstall."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

TASK = "recallhook"
SETTINGS = Path.home() / ".claude" / "settings.json"
KEY = "ANTHROPIC_BASE_URL"


def gateway_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


# -- Claude Code routing --------------------------------------------------
def route_claude(on: bool, port: int, settings: Path = SETTINGS) -> str:
    """Add/remove env.ANTHROPIC_BASE_URL in Claude Code's user settings. Nothing else is touched."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    env = data.get("env") or {}
    ours = gateway_url(port)
    if on:
        if settings.exists():
            bak = settings.with_name(settings.name + ".recallhook.bak")
            if not bak.exists():
                shutil.copy2(settings, bak)
        env[KEY] = ours
        data["env"] = env
        msg = f"routing Claude Code through {ours}"
    else:
        if env.get(KEY) != ours:
            return f"{KEY} is not set to {ours}; left as is"
        env.pop(KEY)
        if env:
            data["env"] = env
        else:
            data.pop("env", None)
        msg = "Claude Code routing removed"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return msg


# -- Claude Code hook (works in the desktop app, VS Code and the terminal) ------
HOOK_MARK = "recallhook.cli hook"


def hook_command() -> str:
    return f'"{Path(sys.executable).as_posix()}" -m {HOOK_MARK}'


def _is_ours(entry: dict) -> bool:
    return any(HOOK_MARK in h.get("command", "") for h in entry.get("hooks", []))


EVENTS = ("UserPromptSubmit", "Stop")  # recall on each prompt; learn from each reply


def install_hook(on: bool, settings: Path = SETTINGS) -> str:
    """Add/remove our hooks in ~/.claude/settings.json (idempotent, other hooks are left alone)."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    if on and settings.exists():
        bak = settings.with_name(settings.name + ".recallhook.bak")
        if not bak.exists():
            shutil.copy2(settings, bak)
    for event in EVENTS:
        entries = [e for e in hooks.get(event, []) if not _is_ours(e)]
        if on:
            entries.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 8}]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "memory hooks installed (recall on each prompt, learn from each reply)" if on else "memory hooks removed"


def has_stop_hook(settings: Path = SETTINGS) -> bool:
    """True if something will send Claude's replies to recallhook (our settings hook or the plugin)."""
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if any(k.startswith("recallhook@") and v for k, v in (data.get("enabledPlugins") or {}).items()):
        return True
    return any(_is_ours(e) for e in data.get("hooks", {}).get("Stop", []))


# -- Windows Task Scheduler ----------------------------------------------
def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True)


def install_task(args: str = "serve") -> str:
    if sys.platform != "win32":
        return ("autostart is Windows-only for now; on Linux/macOS run "
                f"`recallhook {args}` from a systemd/launchd unit")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pyw if pyw.exists() else sys.executable)
    script = f"""
$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '-m recallhook.cli {args}' -WorkingDirectory $env:USERPROFILE
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# heartbeat: restarts the service within a minute if it ever dies (IgnoreNew prevents duplicates)
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
$p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger @($t1,$t2) -Settings $s -Principal $p -Force | Out-Null
Start-ScheduledTask -TaskName '{TASK}'
"""
    r = _ps(script)
    return "task registered and started" if r.returncode == 0 else f"FAILED: {r.stderr.strip()[:400]}"


def remove_task() -> str:
    if sys.platform != "win32":
        return "nothing to remove"
    r = _ps(f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction Stop")
    return "task removed" if r.returncode == 0 else "no task found"


def listening_pids(port: int) -> set[int]:
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            pids.add(int(parts[4]))
    return pids


def kill_ports(*ports: int) -> list[int]:
    killed = []
    for port in ports:
        for pid in listening_pids(port):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            killed.append(pid)
    return killed


# -- service control -----------------------------------------------------------
def restart_service(port: int) -> str:
    """Stop whatever serves the gateway port so the (re)installed code loads, then start the task."""
    killed = kill_ports(port)
    if sys.platform != "win32":
        return f"stopped {len(killed)} process(es); start the gateway again with `recallhook serve`"
    r = _ps(f"Start-ScheduledTask -TaskName '{TASK}' -ErrorAction Stop")
    return "service restarted" if r.returncode == 0 else (
        "service stopped; no autostart task found (run `recallhook install`, or `recallhook serve`)")


def pause_service(port: int) -> None:
    """Stop the gateway AND stop the heartbeat from restarting it (needed while the store is rebuilt)."""
    if sys.platform == "win32":
        _ps(f"Disable-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue | Out-Null; "
            f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue")
    kill_ports(port)


def resume_service(port: int) -> None:
    if sys.platform == "win32":
        _ps(f"Enable-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue | Out-Null; "
            f"Start-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue")


def hook_state(settings: Path = SETTINGS) -> tuple[str, str]:
    """('ok' | 'duplicate' | 'missing' | 'broken', detail) for our UserPromptSubmit hook.

    The hook can come from ~/.claude/settings.json (`recallhook install`) or from the
    recallhook Claude Code plugin; both at once is harmless (memories are injected once)
    but wasteful, so it is reported as 'duplicate'."""
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing", f"no readable {settings}"
    plugin = any(k.startswith("recallhook@") and v for k, v in (data.get("enabledPlugins") or {}).items())
    state, detail = "missing", "run `recallhook install` (or install the recallhook plugin)"
    for entry in data.get("hooks", {}).get("UserPromptSubmit", []):
        for h in entry.get("hooks", []):
            cmd = h.get("command", "")
            if HOOK_MARK in cmd:
                exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split()[0]
                if Path(exe).exists():
                    state, detail = "ok", cmd
                else:
                    state, detail = ("broken", f"the Python it points to no longer exists: {exe} "
                                               f"(run `recallhook install` again)")
    if plugin and state == "ok":
        return "duplicate", ("both the settings hook and the recallhook plugin are active; keep one: "
                             "`recallhook hooks off` keeps the plugin")
    if plugin and state == "missing":
        return "ok", "provided by the recallhook plugin"
    return state, detail
