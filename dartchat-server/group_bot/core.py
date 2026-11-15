"""The group rules, independent of how messages arrive.

Ported from the owner's Signal group bot: repost check, same news from
another site, the watched-name counter, commands, the midnight report and
Home Assistant numbers. Every rule only runs in groups where the owner
switched it on.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiohttp

from .links import canonical_key, extract_urls, is_shortener
from .news import NewsStore, is_social, mentions, title_from_html
from .rules import RoomRules, Settings
from .stats import Stats, format_day, format_top
from .store import Store

log = logging.getLogger("group_bot")


@dataclass
class Message:
    room_id: str
    event_id: str
    sender: str
    sender_name: str
    body: str
    timestamp: int  # ms since epoch
    reply_to_sender: str | None = None
    is_media: bool = False


def puta(n: int) -> str:
    return "put" if n % 10 == 1 and n % 100 != 11 else "puta"


def count_line(template: str, name: str, count: int) -> str:
    try:
        return template.format(name=name, count=count, puta=puta(count))
    except (KeyError, IndexError, ValueError):
        return template


def fill(template: str, **values) -> str:
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return template


def without_quote(body: str) -> str:
    """Drop the quoted lines that some apps put above a reply ("> ...")."""
    lines = body.splitlines()
    while lines and lines[0].startswith(">"):
        lines.pop(0)
    if lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


class Bot:
    def __init__(self, settings: Settings, rules: RoomRules, store: Store, send,
                 http: aiohttp.ClientSession | None = None):
        """[send] is an async function (room_id, text, reply_to_event_id) -> bool."""
        self.cfg = settings
        self.rules = rules
        self.store = store
        self.send = send
        self.http = http
        self.stats = Stats(store.db)
        self.news = NewsStore(store.db)
        self.sensors = None
        self.sensors_dirty = True
        self.ready = True
        self._titles: dict[str, str | None] = {}

    def local(self, ts_ms: int) -> datetime:
        return datetime.fromtimestamp(ts_ms / 1000, tz=self.cfg.tz)

    def today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    def keyword_label(self, room_id: str) -> str:
        if self.cfg.keyword_stems and self.rules.on(room_id, "keyword"):
            return self.cfg.keyword_name
        return ""

    # -- short links and headlines ----------------------------------------------
    async def expand(self, url: str) -> str:
        cached = self.store.get_expansion(url)
        if cached:
            return cached
        try:
            async with self.http.get(
                url, allow_redirects=True, max_redirects=8,
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"User-Agent": "Mozilla/5.0 (compatible; dartchat-bot)"},
            ) as resp:
                target = str(resp.url)
        except Exception as e:
            log.info("could not expand a short link: %s", e)
            return url
        if "/accounts/login" in target or "/login" in target.split("?")[0]:
            return url
        self.store.put_expansion(url, target)
        return target

    async def key_for(self, url: str) -> str:
        if self.cfg.expand_short and self.http and is_shortener(url):
            url = await self.expand(url)
        return canonical_key(url)

    async def fetch_title(self, url: str) -> str | None:
        if not self.http:
            return None
        if url in self._titles:
            return self._titles[url]
        title = None
        try:
            async with self.http.get(
                url, allow_redirects=True, max_redirects=8,
                timeout=aiohttp.ClientTimeout(total=8),
                headers={"User-Agent": "Mozilla/5.0 (compatible; dartchat-bot)",
                         "Accept-Language": "sr,en;q=0.8"},
            ) as resp:
                if resp.status < 400 and "html" in resp.headers.get("Content-Type", "html"):
                    raw = await resp.content.read(400_000)
                    title = title_from_html(raw.decode(resp.charset or "utf-8", "replace"))
        except Exception as e:
            log.info("could not fetch a headline: %s", e)
        if len(self._titles) > 200:
            self._titles.clear()
        self._titles[url] = title
        return title

    # -- messages -----------------------------------------------------------------
    async def handle(self, m: Message) -> None:
        room = m.room_id
        if not self.rules.active(room):
            return
        on = self.rules.get(room)
        text = without_quote(m.body or "").strip()
        command = text.lower()

        if on["commands"]:
            if command in self.cfg.list_commands:
                await self.post_list(room)
                return
            if self.cfg.keyword_stems and on["keyword"] and command in self.cfg.keyword_commands:
                await self.post_list(room, keyword=True)
                return
            if command in self.cfg.stats_commands:
                await self.send(room, format_day(
                    self.stats.day_stats(room, self.today().isoformat()),
                    "📊 Statistika za danas,", self.keyword_label(room)), None)
                return
            if command in self.cfg.top_commands:
                await self.send(room, format_top(
                    self.stats.top_stats(room, self.today().isoformat()),
                    "🏆 Top lista za danas,", self.keyword_label(room)), None)
                return

        urls = [] if m.is_media else extract_urls(text)
        dupes = await self.check_links(m, urls) if urls and on["repost"] else []
        same_news = None
        if urls and not dupes and on["same_news"]:
            same_news = await self.check_news(m, urls)
        keyword = bool(urls) and on["keyword"] and await self.check_keyword(urls)

        # Statistics: counts only, never the text.
        when = self.local(m.timestamp)
        self.stats.remember_name(m.sender, m.sender_name)
        self.stats.record(room, m.sender, m.timestamp, when.date().isoformat(), when.hour,
                          bool(urls), bool(dupes or same_news), m.reply_to_sender, keyword)
        self.sensors_dirty = True

        keyword_line = ""
        if keyword:
            count = self.store.add(room, m.sender, m.sender_name, "keyword")
            if self.cfg.keyword_count_text:
                keyword_line = count_line(self.cfg.keyword_count_text, m.sender_name, count)
        if dupes:
            count = self.store.add(room, m.sender, m.sender_name)
            await self.reply_repost(m, count, dupes[0], keyword_line)
        elif same_news:
            count = self.store.add(room, m.sender, m.sender_name)
            msg = fill(random.choice(self.cfg.news_replies), name=same_news.author_name,
                       title=same_news.title,
                       date=self.local(same_news.timestamp).strftime("%d.%m.%Y"))
            if self.cfg.count_text:
                msg += "\n" + count_line(self.cfg.count_text, m.sender_name, count)
            if keyword_line:
                msg += "\n" + keyword_line
            # Reply to the first post of the story when it is still known.
            await self.send(room, msg, self._first_event(room, same_news.key) or m.event_id)
        elif keyword:
            msg = fill(random.choice(self.cfg.keyword_replies), name=m.sender_name)
            if keyword_line:
                msg += "\n" + keyword_line
            await self.send(room, msg, m.event_id)

    def _first_event(self, room: str, key: str) -> str | None:
        row = self.store.db.execute(
            "SELECT event_id FROM links WHERE room_id = ? AND key = ?", (room, key)).fetchone()
        return row[0] if row else None

    def redacted(self, room_id: str, event_id: str) -> None:
        if self.store.forget_event(room_id, event_id):
            log.info("a deleted message's links were forgotten")

    async def check_keyword(self, urls: list[str]) -> bool:
        for url in urls:
            if mentions(self.cfg.keyword_stems, None, url):
                return True
            if not is_social(url) and mentions(self.cfg.keyword_stems,
                                               await self.fetch_title(url)):
                return True
        return False

    async def check_news(self, m: Message, urls: list[str]):
        seen = set()
        for url in urls:
            if is_social(url):
                continue
            key = await self.key_for(url)
            if key in seen:
                continue
            seen.add(key)
            # Remember the link so the reply can quote the first post later.
            if not self.rules.on(m.room_id, "repost"):
                self.store.check_and_add(m.room_id, key, url, m.sender, m.sender_name,
                                         m.timestamp, m.event_id)
            title = await self.fetch_title(url)
            if not title:
                continue
            match = self.news.find_and_add(m.room_id, key, url, title, m.sender,
                                           m.sender_name, m.timestamp,
                                           self.cfg.news_window_days, self.cfg.news_threshold)
            if match:
                return match
        return None

    async def check_links(self, m: Message, urls: list[str]) -> list:
        dupes = []
        seen = set()
        for url in urls:
            key = await self.key_for(url)
            if key in seen:
                continue
            seen.add(key)
            first = self.store.check_and_add(m.room_id, key, url, m.sender, m.sender_name,
                                             m.timestamp, m.event_id)
            if first and first.event_id == m.event_id:
                continue  # the same message delivered again
            if first:
                dupes.append(first)
        return dupes

    async def reply_repost(self, m: Message, count: int, first, extra: str = "") -> None:
        day = self.local(first.timestamp).strftime("%d.%m.%Y")
        options = self.cfg.self_replies if first.author == m.sender else self.cfg.replies
        msg = fill(random.choice(options), name=first.author_name, date=day, url=first.url)
        if self.cfg.count_text:
            msg += "\n" + count_line(self.cfg.count_text, m.sender_name, count)
        if extra:
            msg += "\n" + extra
        await self.send(m.room_id, msg, first.event_id)

    async def post_list(self, room: str, keyword: bool = False) -> None:
        if keyword:
            rows, title = self.store.top(room, table="keyword"), self.cfg.keyword_title
            template = self.cfg.keyword_count_text or "{name}: {count}"
        else:
            rows, title = self.store.top(room), self.cfg.list_title
            template = self.cfg.count_text or "{name}: {count}"
        lines = [title] + ([f"{i}. " + count_line(template, n, c)
                            for i, (n, c) in enumerate(rows, 1)] or ["-"])
        await self.send(room, "\n".join(lines), None)

    # -- midnight report and sensors --------------------------------------------
    report_delays = (3, 20, 60, 180)

    async def send_reliably(self, room: str, text: str) -> bool:
        for delay in self.report_delays:
            await asyncio.sleep(delay)
            if await self.send(room, text, None):
                return True
        log.error("midnight report: gave up sending")
        return False

    async def tick(self) -> None:
        """Called every ~30 s: midnight report and sensor refresh."""
        today = self.today()
        yesterday = (today - timedelta(days=1)).isoformat()
        last = self.stats.get_state("last_report_day")
        if last is None:
            self.stats.set_state("last_report_day", yesterday)
        elif last < yesterday and self.ready:
            self.stats.set_state("last_report_day", yesterday)
            for room in self.rules.active_rooms():
                if not self.rules.on(room, "daily_report"):
                    continue
                if not self.stats.day_stats(room, yesterday).messages:
                    continue
                label = self.keyword_label(room)
                for text in (format_day(self.stats.day_stats(room, yesterday),
                                        keyword_label=label),
                             format_top(self.stats.top_stats(room, yesterday),
                                        keyword_label=label)):
                    await self.send_reliably(room, text)
            self.sensors_dirty = True
        if self.sensors and self.sensors_dirty:
            self.sensors_dirty = False
            rooms = [r for r in self.rules.active_rooms() if self.rules.on(r, "ha_sensors")]
            await self.sensors.update(rooms, today.isoformat())


def slug(text: str) -> str:
    from .news import CYR, LAT
    plain = "".join(CYR.get(ch, ch) for ch in text).translate(LAT).lower()
    return re.sub(r"[^a-z0-9]+", "_", plain).strip("_")[:40]
