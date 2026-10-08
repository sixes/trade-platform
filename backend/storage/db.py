from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS iv_snapshots (
    underlying TEXT NOT NULL,
    date TEXT NOT NULL,
    spot REAL,
    atm_iv REAL,
    skew_ratio REAL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (underlying, date)
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    scenario TEXT,
    symbols TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS iv_history (
    underlying TEXT NOT NULL,
    source TEXT NOT NULL,
    date TEXT NOT NULL,
    iv REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (underlying, source, date)
);
CREATE TABLE IF NOT EXISTS watchlist_iv (
    underlying TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        log.info("SQLite database ready at %s", path)

    def upsert_snapshot(self, underlying: str, day: str, spot: Optional[float], atm_iv: Optional[float], skew_ratio: Optional[float]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO iv_snapshots (underlying, date, spot, atm_iv, skew_ratio, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(underlying, date) DO UPDATE SET
                     spot=excluded.spot, atm_iv=excluded.atm_iv, skew_ratio=excluded.skew_ratio, created_at=excluded.created_at""",
                (underlying, day, spot, atm_iv, skew_ratio, datetime.now().isoformat(timespec="seconds")),
            )
            self._conn.commit()

    def atm_iv_history(self, underlying: str, lookback_days: int = 252) -> List[Tuple[str, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, atm_iv FROM iv_snapshots WHERE underlying=? AND atm_iv IS NOT NULL ORDER BY date DESC LIMIT ?",
                (underlying, lookback_days),
            ).fetchall()
        return [(r["date"], float(r["atm_iv"])) for r in reversed(rows)]

    def replace_iv_history(self, underlying: str, source: str, points: List[Tuple[str, float]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute("DELETE FROM iv_history WHERE underlying=? AND source=?", (underlying, source))
            self._conn.executemany(
                "INSERT OR REPLACE INTO iv_history (underlying, source, date, iv, created_at) VALUES (?, ?, ?, ?, ?)",
                [(underlying, source, day, float(iv), now) for day, iv in points],
            )
            self._conn.commit()

    def iv_history(self, underlying: str, source: str, lookback_days: int = 252) -> List[Tuple[str, float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, iv FROM iv_history WHERE underlying=? AND source=? ORDER BY date DESC LIMIT ?",
                (underlying, source, lookback_days),
            ).fetchall()
        return [(r["date"], float(r["iv"])) for r in reversed(rows)]

    def iv_history_sources(self, underlying: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, MAX(created_at) AS latest FROM iv_history WHERE underlying=? GROUP BY source ORDER BY latest DESC",
                (underlying,),
            ).fetchall()
        return [r["source"] for r in rows]

    def save_watchlist_iv(self, underlying: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO watchlist_iv (underlying, updated_at, payload) VALUES (?, ?, ?)",
                (underlying, datetime.now().isoformat(timespec="seconds"), json.dumps(payload, default=str)),
            )
            self._conn.commit()

    def watchlist_iv(self, underlyings: List[str]) -> dict:
        if not underlyings:
            return {}
        marks = ",".join("?" for _ in underlyings)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT underlying, updated_at, payload FROM watchlist_iv WHERE underlying IN ({marks})", tuple(underlyings)
            ).fetchall()
        return {r["underlying"]: dict(json.loads(r["payload"]), updated_at=r["updated_at"]) for r in rows}

    def save_scan(self, payload: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO scans (created_at, scenario, symbols, payload) VALUES (?, ?, ?, ?)",
                (
                    payload.get("finished_at") or datetime.now().isoformat(timespec="seconds"),
                    payload.get("scenario", {}).get("scenario") if isinstance(payload.get("scenario"), dict) else payload.get("scenario"),
                    ",".join(payload.get("symbols", [])),
                    json.dumps(payload, default=str),
                ),
            )
            self._conn.execute("DELETE FROM scans WHERE id NOT IN (SELECT id FROM scans ORDER BY id DESC LIMIT 50)")
            self._conn.commit()
            return int(cur.lastrowid)

    def latest_scan(self) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["payload"]) if row else None
