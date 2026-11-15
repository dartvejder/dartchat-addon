"""Spot the same news story posted from a different site, by comparing headlines.

Ported from the owner's Signal group bot.

Only headlines (link titles) are stored, never message text. Headlines are
normalised so that Cyrillic and Latin, diacritics and Serbian word endings
don't get in the way:

  "Влада најавила изборе за децембар - N1"  ->  {vlada, najav, izbor, decem}
  "Izbori u decembru, najavila Vlada | Blic" ->  {izbor, decem, najav, vlada}
"""
from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

# Social sites: their "titles" are captions or "Instagram", not headlines.
SOCIAL = {
    "instagram.com", "x.com", "twitter.com", "tiktok.com", "facebook.com", "fb.watch",
    "youtube.com", "youtu.be", "reddit.com", "redd.it", "bsky.app", "threads.net",
    "open.spotify.com", "t.me", "linkedin.com", "pinterest.com", "pin.it",
    "vm.tiktok.com", "vt.tiktok.com", "t.co", "music.youtube.com", "m.youtube.com",
    # Chat apps and app stores: every link shares one generic title
    # ("Join ... on Viber"), which would look like the same story.
    "viber.com", "vb.me", "whatsapp.com", "wa.me", "telegram.me", "telegram.org",
    "discord.gg", "discord.com", "signal.group", "signal.me", "messenger.com", "m.me",
    "apps.apple.com", "play.google.com", "maps.google.com", "maps.app.goo.gl",
}

CYR = dict(zip(
    "абвгдђежзијклљмнњопрстћуфхцчџшАБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ",
    ["a", "b", "v", "g", "d", "dj", "e", "z", "z", "i", "j", "k", "l", "lj", "m", "n", "nj",
     "o", "p", "r", "s", "t", "c", "u", "f", "h", "c", "c", "dz", "s"] * 2,
))
LAT = str.maketrans({"č": "c", "ć": "c", "ž": "z", "š": "s", "đ": "dj",
                     "Č": "c", "Ć": "c", "Ž": "z", "Š": "s", "Đ": "dj"})

STOP = set("""
a i u o na za je su se da od do sa iz ili ali kao sto što koji koja koje kod pre posle nakon
bio bila bilo biti ce će cu ćе nije nisu jos još vec već samo sve svi ovo ono taj ta to te
the a an of to in on for and or is are was were be by with at from as that this it its
video foto uzivo uživo live news vesti najnovije ekskluzivno breaking update
""".split())

STEM_LEN = 5  # crude Serbian "stemming": izbori/izbore/izborima -> izbor


def is_social(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    host = host[2:] if host.startswith("m.") else host
    return host in SOCIAL or any(host.endswith("." + s) for s in SOCIAL)


def clean_title(title: str) -> str:
    """Drop the site name: 'Headline - N1' / 'Headline | Blic' -> 'Headline'."""
    title = html.unescape(title or "").strip()
    parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+|\s*\|\s*", title) if p.strip()]
    return max(parts, key=len) if parts else ""


def tokens(title: str) -> set[str]:
    text = "".join(CYR.get(ch, ch) for ch in clean_title(title)).translate(LAT).lower()
    words = re.findall(r"[a-z0-9]+", text)
    out = set()
    for w in words:
        if w in STOP or (len(w) < 3 and not w.isdigit()):
            continue
        out.add(w if w.isdigit() else w[:STEM_LEN])
    return out


def similarity(a: set[str], b: set[str]) -> float:
    """Share of the shorter headline's words that appear in the other one."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def same_story(a: set[str], b: set[str], threshold: float = 0.6, min_common: int = 3) -> bool:
    return len(a & b) >= min_common and similarity(a, b) >= threshold


def keyword_stems(words: str) -> list[tuple[str, str]]:
    """'Đoković, Ђоковић' -> [("djoko", "djokovic")]: (headline stem, plain spelling)."""
    out = []
    for word in re.split(r"[,;|]", words or ""):
        plain = "".join(CYR.get(ch, ch) for ch in word.strip()).translate(LAT).lower()
        plain = re.sub(r"[^a-z0-9]", "", plain)
        if len(plain) >= 3:
            pair = (plain if plain.isdigit() else plain[:STEM_LEN], plain)
            if pair not in out:
                out.append(pair)
    return out


def mentions(stems: list[tuple[str, str]], title: str | None, url: str = "") -> bool:
    """Is this link about one of the watched words? Checks the headline and the URL."""
    if not stems:
        return False
    if title:
        found = tokens(title)
        if any(stem in found for stem, _ in stems):
            return True
    from urllib.parse import unquote
    parts = urlsplit(url)
    slug = unquote(parts.path + "?" + parts.query).lower()
    slug = "".join(CYR.get(ch, ch) for ch in slug).translate(LAT)
    return any(plain in slug for _, plain in stems)


_TITLE_RE = [
    re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:title', re.I),
    re.compile(r'<meta[^>]+name=["\']twitter:title["\'][^>]*content=["\']([^"\']+)', re.I),
    re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S),
]


def title_from_html(page: str) -> str | None:
    for rx in _TITLE_RE:
        m = rx.search(page)
        if m and m.group(1).strip():
            return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return None


@dataclass
class Headline:
    url: str
    title: str
    author: str
    author_name: str
    timestamp: int
    key: str


class NewsStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        db.execute(
            """CREATE TABLE IF NOT EXISTS headlines (
                   group_id    TEXT NOT NULL,
                   key         TEXT NOT NULL,
                   url         TEXT NOT NULL,
                   title       TEXT NOT NULL,
                   tokens      TEXT NOT NULL,
                   author      TEXT NOT NULL,
                   author_name TEXT NOT NULL,
                   timestamp   INTEGER NOT NULL,
                   PRIMARY KEY (group_id, key)
               )""")
        db.commit()

    def find_and_add(self, group: str, key: str, url: str, title: str, author: str,
                     author_name: str, ts: int, window_days: float,
                     threshold: float) -> Headline | None:
        """Return an earlier headline about the same story, else remember this one."""
        toks = tokens(title)
        if len(toks) < 3:
            return None  # too short to compare safely
        since = ts - int(window_days * 86400 * 1000)
        best, best_score = None, 0.0
        for row in self.db.execute(
                "SELECT url, title, tokens, author, author_name, timestamp, key FROM headlines "
                "WHERE group_id = ? AND timestamp >= ? AND key != ? ORDER BY timestamp",
                (group, since, key)):
            other = set(row[2].split())
            if same_story(toks, other, threshold):
                score = similarity(toks, other)
                if score > best_score:
                    best, best_score = Headline(row[0], row[1], row[3], row[4], row[5], row[6]), score
        if best is None:
            self.db.execute(
                "INSERT OR IGNORE INTO headlines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (group, key, url, clean_title(title), " ".join(sorted(toks)), author,
                 author_name, ts))
            # keep the table small
            self.db.execute("DELETE FROM headlines WHERE timestamp < ?",
                            (ts - 30 * 86400 * 1000,))
            self.db.commit()
        return best
