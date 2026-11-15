"""Group activity statistics. Only counts are kept, never message text.

Ported from the owner's Signal group bot.

For every group message we store: who, when (day and hour), whether it had a
link, whether it was a repost, and whom it replied to.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class DayStats:
    day: str
    messages: int = 0
    people: int = 0
    links: int = 0
    reposts: int = 0
    replies: int = 0
    keyword: int = 0
    busiest_hour: int | None = None
    total_messages: int = 0  # all time


@dataclass
class TopStats:
    day: str
    posters: list[tuple[str, int]] = field(default_factory=list)
    replied_to: tuple[str, int] | None = None
    linker: tuple[str, int] | None = None
    reposter: tuple[str, int] | None = None
    keyword: tuple[str, int] | None = None


class Stats:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity (
                group_id  TEXT NOT NULL,
                author    TEXT NOT NULL,
                ts        INTEGER NOT NULL,
                day       TEXT NOT NULL,      -- local date, YYYY-MM-DD
                hour      INTEGER NOT NULL,   -- local hour, 0-23
                has_link  INTEGER NOT NULL DEFAULT 0,
                is_repost INTEGER NOT NULL DEFAULT 0,
                reply_to  TEXT,
                is_keyword INTEGER NOT NULL DEFAULT 0,
                UNIQUE (group_id, author, ts)
            );
            CREATE INDEX IF NOT EXISTS activity_day ON activity (group_id, day);
            CREATE TABLE IF NOT EXISTS people (
                author TEXT PRIMARY KEY,
                name   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        db.commit()

    # -- recording ------------------------------------------------------------------
    def remember_name(self, author: str, name: str) -> None:
        if author and name:
            self.db.execute(
                "INSERT INTO people (author, name) VALUES (?, ?) "
                "ON CONFLICT (author) DO UPDATE SET name = excluded.name", (author, name))

    def record(self, group: str, author: str, ts: int, day: str, hour: int,
               has_link: bool, is_repost: bool, reply_to: str | None,
               is_keyword: bool = False) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO activity "
            "(group_id, author, ts, day, hour, has_link, is_repost, reply_to, is_keyword) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group, author, ts, day, hour, int(has_link), int(is_repost), reply_to,
             int(is_keyword)))
        self.db.commit()

    # -- state ------------------------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # -- queries -------------------------------------------------------------------------
    def groups_active_on(self, day: str) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT group_id FROM activity WHERE day = ?", (day,))]

    def busiest_group(self) -> str | None:
        row = self.db.execute(
            "SELECT group_id FROM activity GROUP BY group_id ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None

    def _name(self, author: str) -> str:
        row = self.db.execute("SELECT name FROM people WHERE author = ?", (author,)).fetchone()
        return row[0] if row else "nepoznat"

    def day_stats(self, group: str, day: str) -> DayStats:
        s = DayStats(day)
        row = self.db.execute(
            "SELECT COUNT(*), COUNT(DISTINCT author), SUM(has_link), SUM(is_repost), "
            "SUM(reply_to IS NOT NULL), SUM(is_keyword) FROM activity "
            "WHERE group_id = ? AND day = ?", (group, day)).fetchone()
        s.messages, s.people = row[0], row[1]
        s.links, s.reposts, s.replies = (row[2] or 0), (row[3] or 0), (row[4] or 0)
        s.keyword = row[5] or 0
        hour = self.db.execute(
            "SELECT hour FROM activity WHERE group_id = ? AND day = ? "
            "GROUP BY hour ORDER BY COUNT(*) DESC, hour LIMIT 1", (group, day)).fetchone()
        s.busiest_hour = hour[0] if hour else None
        s.total_messages = self.db.execute(
            "SELECT COUNT(*) FROM activity WHERE group_id = ?", (group,)).fetchone()[0]
        return s

    def _top(self, sql: str, args: tuple, limit: int) -> list[tuple[str, int]]:
        return [(self._name(a), c) for a, c in self.db.execute(sql + f" LIMIT {int(limit)}", args)]

    def top_stats(self, group: str, day: str, limit: int = 5) -> TopStats:
        t = TopStats(day)
        base = "FROM activity WHERE group_id = ? AND day = ?"
        t.posters = self._top(
            f"SELECT author, COUNT(*) c {base} GROUP BY author ORDER BY c DESC, author",
            (group, day), limit)
        for attr, sql in (
            ("replied_to", f"SELECT reply_to, COUNT(*) c {base} AND reply_to IS NOT NULL "
                           "GROUP BY reply_to ORDER BY c DESC, reply_to"),
            ("linker", f"SELECT author, SUM(has_link) c {base} AND has_link = 1 "
                       "GROUP BY author ORDER BY c DESC, author"),
            ("reposter", f"SELECT author, SUM(is_repost) c {base} AND is_repost = 1 "
                         "GROUP BY author ORDER BY c DESC, author"),
            ("keyword", f"SELECT author, SUM(is_keyword) c {base} AND is_keyword = 1 "
                      "GROUP BY author ORDER BY c DESC, author"),
        ):
            top = self._top(sql, (group, day), 1)
            setattr(t, attr, top[0] if top else None)
        return t


# -- message formatting (Serbian) -----------------------------------------------------

def _date(day: str) -> str:
    y, m, d = day.split("-")
    return f"{d}.{m}.{y}."


def format_day(s: DayStats, title: str = "📊 Statistika za", keyword_label: str = "") -> str:
    lines = [f"{title} {_date(s.day)}", f"Poruka: {s.messages}"]
    if s.messages:
        lines += [
            f"Pisalo je: {s.people}",
            f"Linkova: {s.links} (ponovljenih: {s.reposts})",
            f"Odgovora: {s.replies}",
        ]
        if keyword_label:
            lines.append(f"{keyword_label}: {s.keyword}")
        if s.busiest_hour is not None:
            lines.append(f"Najaktivnije: {s.busiest_hour:02d}-{(s.busiest_hour + 1) % 24:02d}h")
    lines.append(f"Ukupno do sada: {s.total_messages}")
    return "\n".join(lines)


def format_top(t: TopStats, title: str = "🏆 Top lista za", keyword_label: str = "") -> str:
    lines = [f"{title} {_date(t.day)}"]
    if not t.posters:
        return "\n".join(lines + ["Niko nije pisao."])
    lines.append("Najviše poruka:")
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, count) in enumerate(t.posters):
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {name}: {count}")
    if t.replied_to:
        lines.append(f"💬 Najviše odgovora dobio/la: {t.replied_to[0]} ({t.replied_to[1]})")
    if t.linker:
        lines.append(f"🔗 Najviše linkova: {t.linker[0]} ({t.linker[1]})")
    if t.reposter:
        lines.append(f"🙈 Ponavljač dana: {t.reposter[0]} ({t.reposter[1]})")
    if t.keyword and keyword_label:
        lines.append(f"🙉 {keyword_label}: {t.keyword[0]} ({t.keyword[1]})")
    return "\n".join(lines)
