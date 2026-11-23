"""SQLite storage: one file, WAL mode, versioned migrations, small repository API."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE channels (
        id INTEGER PRIMARY KEY,
        login TEXT NOT NULL UNIQUE COLLATE NOCASE,
        twitch_id TEXT,
        relation TEXT NOT NULL DEFAULT 'other',
        added_at REAL NOT NULL
    );
    CREATE TABLE analyses (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        video_path TEXT,
        chat_path TEXT,
        chat_offset_s REAL NOT NULL DEFAULT 0,
        duration_s REAL,
        message_count INTEGER,
        params_json TEXT,
        status TEXT NOT NULL DEFAULT 'done',
        created_at REAL NOT NULL
    );
    CREATE TABLE highlights (
        id INTEGER PRIMARY KEY,
        analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
        rank INTEGER NOT NULL,
        burst_start REAL NOT NULL,
        peak_t REAL NOT NULL,
        score REAL NOT NULL,
        unique_at_peak INTEGER NOT NULL,
        event_t REAL NOT NULL,
        window_start REAL NOT NULL,
        window_end REAL NOT NULL,
        clamped INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX highlights_analysis ON highlights(analysis_id, rank);
    CREATE TABLE clips (
        id INTEGER PRIMARY KEY,
        analysis_id INTEGER REFERENCES analyses(id) ON DELETE SET NULL,
        highlight_id INTEGER REFERENCES highlights(id) ON DELETE SET NULL,
        path TEXT NOT NULL,
        orientation TEXT NOT NULL,
        window_start REAL NOT NULL,
        window_end REAL NOT NULL,
        duration_s REAL,
        encoder TEXT,
        score REAL,
        replaced INTEGER NOT NULL DEFAULT 0,
        twitch_clip_id TEXT,
        edit_url TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX clips_created ON clips(created_at DESC);
    CREATE TABLE kv (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # v2: where a clip came from ("export", "import"); for imports, the file it was copied from
    """
    ALTER TABLE clips ADD COLUMN source TEXT NOT NULL DEFAULT 'export';
    ALTER TABLE clips ADD COLUMN original_path TEXT
    """,
]


class Database:
    """Thread-safe enough for this app: one connection, serialized by a lock."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @property
    def version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        with self.lock:
            current = self.version
            for i, script in enumerate(MIGRATIONS[current:], start=current + 1):
                self.conn.execute("BEGIN")
                try:
                    for statement in _split(script):   # executescript() would commit mid-transaction
                        self.conn.execute(statement)
                    self.conn.execute(f"PRAGMA user_version = {i}")
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    # --- key/value -----------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.query("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row[0]["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value)))

    # --- channels ------------------------------------------------------------
    def add_channel(self, login: str, relation: str = "other") -> int:
        login = login.strip().lower()
        if not login or not login.replace("_", "").isalnum() or len(login) > 25:
            raise ValueError(f"not a valid Twitch login: {login!r}")
        cur = self.execute("INSERT OR IGNORE INTO channels(login, relation, added_at) VALUES(?, ?, ?)",
                           (login, relation, time.time()))
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        return self.query("SELECT id FROM channels WHERE login = ?", (login,))[0]["id"]

    def remove_channel(self, login: str) -> None:
        self.execute("DELETE FROM channels WHERE login = ?", (login.strip().lower(),))

    def channels(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM channels ORDER BY login")

    # --- analyses / highlights ----------------------------------------------
    def save_analysis(self, kind: str, title: str, video_path: str | None, chat_path: str | None,
                      chat_offset_s: float, duration_s: float | None, message_count: int,
                      params: dict, highlights: list[dict]) -> int:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                cur = self.conn.execute(
                    "INSERT INTO analyses(kind, title, video_path, chat_path, chat_offset_s, duration_s, "
                    "message_count, params_json, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (kind, title, video_path, chat_path, chat_offset_s, duration_s, message_count,
                     json.dumps(params), time.time()))
                aid = cur.lastrowid
                for h in highlights:
                    hc = self.conn.execute(
                        "INSERT INTO highlights(analysis_id, rank, burst_start, peak_t, score, unique_at_peak, "
                        "event_t, window_start, window_end, clamped) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (aid, h["rank"], h["burst_start"], h["peak_t"], h["score"], h["unique_at_peak"],
                         h["event_t"], h["window_start"], h["window_end"], int(h.get("clamped", False))))
                    h["id"] = hc.lastrowid
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return aid

    def analyses(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM analyses ORDER BY created_at DESC LIMIT ?", (limit,))

    def highlights(self, analysis_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM highlights WHERE analysis_id = ? ORDER BY rank", (analysis_id,))

    # --- clips -----------------------------------------------------------------
    def add_clip(self, **kw: Any) -> int:
        kw.setdefault("created_at", time.time())
        cols = ", ".join(kw)
        marks = ", ".join("?" for _ in kw)
        return self.execute(f"INSERT INTO clips({cols}) VALUES({marks})", kw.values()).lastrowid

    def clips(self, include_replaced: bool = False, limit: int = 500) -> list[sqlite3.Row]:
        where = "" if include_replaced else "WHERE c.replaced = 0"
        return self.query(
            "SELECT c.*, a.title AS analysis_title FROM clips c LEFT JOIN analyses a ON a.id = c.analysis_id "
            f"{where} ORDER BY c.created_at DESC LIMIT ?", (limit,))

    def clip(self, clip_id: int) -> sqlite3.Row | None:
        rows = self.query("SELECT * FROM clips WHERE id = ?", (clip_id,))
        return rows[0] if rows else None

    def delete_clip(self, clip_id: int) -> None:
        self.execute("DELETE FROM clips WHERE id = ?", (clip_id,))

    def stats(self) -> dict:
        r = self.query("SELECT (SELECT COUNT(*) FROM clips WHERE replaced = 0) AS clips, "
                       "(SELECT COUNT(*) FROM analyses) AS analyses, (SELECT COUNT(*) FROM channels) AS channels")[0]
        return dict(r)


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
