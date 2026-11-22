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


def install_hook(on: bool, settings: Path = SETTINGS) -> str:
    """Add/remove our UserPromptSubmit hook in ~/.claude/settings.json (idempotent)."""
    data = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
    hooks = data.setdefault("hooks", {})
    entries = [e for e in hooks.get("UserPromptSubmit", []) if not _is_ours(e)]
    if on:
        if settings.exists():
            bak = settings.with_name(settings.name + ".recallhook.bak")
            if not bak.exists():
                shutil.copy2(settings, bak)
        entries.append({"hooks": [{"type": "command", "command": hook_command(), "timeout": 8}]})
        msg = "memory hook installed (UserPromptSubmit)"
    else:
        msg = "memory hook removed"
    if entries:
        hooks["UserPromptSubmit"] = entries
    else:
        hooks.pop("UserPromptSubmit", None)
    if not hooks:
        data.pop("hooks", None)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return msg


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
