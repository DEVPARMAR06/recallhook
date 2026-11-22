"""Keeps a Headroom proxy running next to the gateway (health-checked, not PID-checked)."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("recallhook")
GRACE = 90  # seconds Headroom may take to boot (it loads a compression model)


def find_headroom() -> str | None:
    exe = shutil.which("headroom")
    if exe:
        return exe
    for cand in (Path(sys.executable).parent / "Scripts" / "headroom.exe",
                 Path(sys.executable).parent / "headroom"):
        if cand.exists():
            return str(cand)
    return None


def healthy(upstream: str) -> bool:
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/livez", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


class Supervisor(threading.Thread):
    """Respawns Headroom when /livez stops answering. Never kills a healthy one,
    including an orphan left behind by a previous gateway."""

    def __init__(self, upstream: str, log_path: Path, interval: float = 10.0):
        super().__init__(name="headroom-supervisor", daemon=True)
        self.upstream, self.log_path, self.interval = upstream, log_path, interval
        self.port = upstream.rsplit(":", 1)[-1].split("/")[0]
        self.child: subprocess.Popen | None = None
        self.started_at = 0.0
        self.failures = 0
        self._stop = threading.Event()

    def spawn(self) -> None:
        exe = find_headroom()
        if not exe:
            log.error("headroom not found; install with: pip install headroom-ai[proxy]")
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(self.log_path, "ab") as out:
            self.child = subprocess.Popen([exe, "proxy", "--port", self.port], stdout=out,
                                          stderr=out, stdin=subprocess.DEVNULL, env=env,
                                          creationflags=flags)
        self.started_at = time.time()
        log.info("started headroom (pid %s)", self.child.pid)

    def tick(self) -> None:
        if healthy(self.upstream):
            self.failures = 0
            return
        booting = self.child and self.child.poll() is None and time.time() - self.started_at < GRACE
        if booting:
            return
        self.failures += 1
        if self.child and self.child.poll() is None:
            log.warning("headroom unresponsive; restarting")
            self.child.terminate()
        if self.failures > 1:  # back off if it keeps dying (e.g. port held by a stranger)
            time.sleep(min(60, 5 * self.failures))
        self.spawn()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log.warning("supervisor error: %s", type(e).__name__)

    def stop(self) -> None:
        self._stop.set()
        if self.child and self.child.poll() is None:
            self.child.terminate()
