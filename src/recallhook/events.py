"""Small persistent activity log (SQLite) that feeds the dashboard.

Stores counts, latencies and short snippets of memories that were recalled/stored.
It never stores the user's prompt text."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

KEEP_DAYS = 90


class EventLog:
    def __init__(self, path: Path | str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("""CREATE TABLE IF NOT EXISTS events(
                ts REAL, kind TEXT, source TEXT, hits INTEGER, ms INTEGER, tokens INTEGER, detail TEXT)""")
            self._db.execute("DELETE FROM events WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
            self._db.commit()

    def add(self, kind: str, source: str = "", hits: int = 0, ms: int = 0,
            tokens: int = 0, detail: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                             (time.time(), kind, source, hits, ms, tokens, detail[:400]))
            self._db.commit()

    def recent(self, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT ts,kind,source,hits,ms,tokens,detail FROM events ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(zip(("ts", "kind", "source", "hits", "ms", "tokens", "detail"), r)) for r in rows]

    def series(self, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        with self._lock:
            rows = self._db.execute(
                """SELECT date(ts,'unixepoch','localtime') d, kind, COUNT(*), COALESCE(SUM(hits),0),
                          COALESCE(SUM(tokens),0) FROM events WHERE ts>=? GROUP BY d, kind""",
                (since,)).fetchall()
        out: dict[str, dict] = {}
        for d, kind, n, hits, tokens in rows:
            row = out.setdefault(d, {"day": d, "recall": 0, "stored": 0, "tokens": 0})
            if kind == "recall":
                row["recall"] += n
                row["tokens"] += tokens
            elif kind == "stored":
                row["stored"] += hits  # hits = memories created
        return [out[k] for k in sorted(out)]

    def totals(self) -> dict:
        with self._lock:
            r = self._db.execute(
                """SELECT COUNT(*), COALESCE(SUM(hits>0),0), COALESCE(AVG(ms),0), COALESCE(SUM(tokens),0)
                   FROM events WHERE kind='recall'""").fetchone()
            s = self._db.execute("SELECT COALESCE(SUM(hits),0) FROM events WHERE kind='stored'").fetchone()
            t = self._db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='recall' AND ts>=?",
                (time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)),)).fetchone()
        n = r[0]
        return {"recalls": n, "recalls_today": t[0], "hit_rate": round(100 * r[1] / n) if n else 0,
                "avg_ms": round(r[2]), "tokens_injected": r[3], "memories_created": s[0]}
