"""SQLite storage: links already seen in each group, and repost counters.

Only links (with who posted them and when) are kept, never message text.
Ported from the owner's Signal group bot.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

COUNTERS = ("reposts", "keyword")


@dataclass
class Seen:
    url: str
    author: str
    author_name: str
    timestamp: int  # ms since epoch
    event_id: str  # the chat message that first posted it, for the reply


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS links (
                room_id     TEXT NOT NULL,
                key         TEXT NOT NULL,
                url         TEXT NOT NULL,
                author      TEXT NOT NULL,
                author_name TEXT NOT NULL,
                timestamp   INTEGER NOT NULL,
                event_id    TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (room_id, key)
            );
            CREATE INDEX IF NOT EXISTS links_event ON links (room_id, event_id);
            CREATE TABLE IF NOT EXISTS reposts (
                room_id     TEXT NOT NULL,
                author      TEXT NOT NULL,
                author_name TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, author)
            );
            CREATE TABLE IF NOT EXISTS keyword (
                room_id     TEXT NOT NULL,
                author      TEXT NOT NULL,
                author_name TEXT NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room_id, author)
            );
            CREATE TABLE IF NOT EXISTS expansions (
                short TEXT PRIMARY KEY, target TEXT NOT NULL, fetched INTEGER NOT NULL
            );
            """
        )
        self.db.commit()

    def check_and_add(self, room_id: str, key: str, url: str, author: str,
                      author_name: str, timestamp: int, event_id: str) -> Seen | None:
        """Return the first sighting if this key was seen before, else record it."""
        row = self.db.execute(
            "SELECT url, author, author_name, timestamp, event_id FROM links "
            "WHERE room_id = ? AND key = ?", (room_id, key),
        ).fetchone()
        if row:
            self.db.execute(
                "UPDATE links SET count = count + 1 WHERE room_id = ? AND key = ?",
                (room_id, key),
            )
            self.db.commit()
            return Seen(*row)
        self.db.execute(
            "INSERT INTO links (room_id, key, url, author, author_name, timestamp, event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room_id, key, url, author, author_name, timestamp, event_id),
        )
        self.db.commit()
        return None

    def forget_event(self, room_id: str, event_id: str) -> int:
        """A deleted message no longer counts as the first post of its links."""
        cursor = self.db.execute(
            "DELETE FROM links WHERE room_id = ? AND event_id = ?", (room_id, event_id))
        self.db.commit()
        return cursor.rowcount

    def add(self, room_id: str, author: str, author_name: str,
            table: str = "reposts") -> int:
        """Count one more for this person; return their new total."""
        assert table in COUNTERS
        self.db.execute(
            f"INSERT INTO {table} (room_id, author, author_name, count) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (room_id, author) DO UPDATE SET count = count + 1, "
            "author_name = excluded.author_name",
            (room_id, author, author_name),
        )
        self.db.commit()
        return self.db.execute(
            f"SELECT count FROM {table} WHERE room_id = ? AND author = ?", (room_id, author),
        ).fetchone()[0]

    def top(self, room_id: str, limit: int = 20,
            table: str = "reposts") -> list[tuple[str, int]]:
        assert table in COUNTERS
        return self.db.execute(
            f"SELECT author_name, count FROM {table} WHERE room_id = ? "
            "ORDER BY count DESC, author_name LIMIT ?", (room_id, limit),
        ).fetchall()

    def get_expansion(self, short: str) -> str | None:
        row = self.db.execute("SELECT target FROM expansions WHERE short = ?", (short,)).fetchone()
        return row[0] if row else None

    def put_expansion(self, short: str, target: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO expansions (short, target, fetched) VALUES (?, ?, ?)",
            (short, target, int(time.time())),
        )
        self.db.commit()
