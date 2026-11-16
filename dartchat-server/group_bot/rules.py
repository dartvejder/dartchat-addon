"""Which rules the owner switched on in which group, and the bot's texts.

Rules are kept per group in a small JSON file. The texts and other settings
come from the add-on configuration, so the owner can change them in Home
Assistant.
"""
from __future__ import annotations

import json
import os
import re
import threading
from zoneinfo import ZoneInfo

from .news import keyword_stems

# The rules the owner can switch on per group, in the order the app shows them.
RULES = ("repost", "same_news", "keyword", "commands", "daily_report", "ha_sensors",
         "birthdays", "signal_bridge")


def describe(settings: "Settings") -> list[dict]:
    """The rules as the app lists them. The keyword rule is left out until
    the owner has chosen words to watch for."""
    out = [
        {"id": "repost", "label": "Repost check",
         "description": f"When a link is posted again, reply “{settings.replies[0]}” "
                        "quoting the first post, with a repost score."},
        {"id": "same_news", "label": "Same news from another site",
         "description": "Catch the same news story posted from a different site, "
                        "by comparing headlines. Counts as a repost."},
    ]
    if settings.keyword_stems:
        out.append({"id": "keyword", "label": settings.keyword_name,
                    "description": f"Links about {settings.keyword_words} get "
                                   f"“{settings.keyword_replies[0]}” and their own score."})
    commands = sorted(settings.list_commands)[:1] + sorted(settings.stats_commands)[:1] \
        + sorted(settings.top_commands)[:1]
    if settings.keyword_stems:
        commands += sorted(settings.keyword_commands)[:1]
    out += [
        {"id": "commands", "label": "Commands",
         "description": "Answer " + ", ".join(commands) + " in the group."},
        {"id": "daily_report", "label": "Midnight report",
         "description": "Right after midnight, post the day's statistics and top list."},
        {"id": "ha_sensors", "label": "Home Assistant sensors",
         "description": "Show this group's numbers for the day as sensors in Home Assistant."},
        {"id": "birthdays", "label": "Birthdays",
         "description": f"At {settings.birthday_time} on a member's birthday, post "
                        f"“{fill_name(settings.birthday_texts[0])}”. People enter their "
                        "own birthday in the app, under My profile."},
    ]
    if settings.signal_bridge_token:
        out.append({"id": "signal_bridge", "label": "Bridge to Signal",
                    "description": "Pass every message and photo between this group and "
                                   "the Signal group, both ways, as “Name: message”. "
                                   "Switch off to stop it at once."})
    return out


def fill_name(template: str) -> str:
    """The birthday text as an example, with a name in it."""
    return template.replace("{name}", "Ana")


def _split(value, default: str) -> list[str]:
    text = default if value is None or str(value).strip() == "" else str(value)
    return [part for part in text.split("|") if part.strip()]


def _commands(value, default: str) -> set[str]:
    text = default if not value else str(value)
    return {c.strip().lower() for c in text.split(",") if c.strip()}


class Settings:
    """The add-on options that shape what the bot says."""

    def __init__(self, options: dict | None = None):
        o = options or {}

        def text(key, default):
            value = o.get(key)
            return default if value is None else str(value)

        def number(key, default):
            try:
                return float(o.get(key)) if o.get(key) not in (None, "") else default
            except (TypeError, ValueError):
                return default

        self.bot_name = text("group_bot_name", "") or "Dart Bot"
        self.replies = _split(o.get("repost_reply_text"), "Idi u cosak!")
        self.self_replies = _split(o.get("self_repost_reply_text"),
                                   "Repostovao si svoj link, idi u cosak duplo!")
        self.count_text = text("repost_count_text", "{name} je repostovao {count} {puta}")
        self.list_commands = _commands(o.get("list_commands"), "!lista,!repeaters")
        self.list_title = text("list_title", "") or "Lista ponavljača:"
        self.stats_commands = _commands(o.get("stats_commands"), "!stats")
        self.top_commands = _commands(o.get("top_commands"), "!top")
        self.expand_short = o.get("expand_short_links", True) not in (False, "0", 0)
        self.news_replies = _split(o.get("news_reply_text"),
                                   "Ovo je već bilo, samo sa drugog sajta 👀")
        self.news_window_days = min(max(number("news_window_days", 3), 0.5), 30)
        self.news_threshold = min(max(number("news_threshold", 0.6), 0.3), 1)
        self.keyword_words = text("keyword_words", "").strip()
        self.keyword_stems = keyword_stems(self.keyword_words)
        self.keyword_name = text("keyword_rule_name", "") or "Watched name"
        self.keyword_replies = _split(o.get("keyword_reply_text"), f"{self.keyword_name}! 🙉")
        self.keyword_count_text = text("keyword_count_text", "{name}: {count}")
        self.keyword_commands = _commands(o.get("keyword_commands"), "!keyword")
        self.keyword_title = f"{self.keyword_name}:"
        # Shared password with the Signal bot add-on; empty hides the bridge.
        self.signal_bridge_token = text("signal_bridge_token", "").strip()
        self.birthday_texts = _split(o.get("birthday_text"), "🎂 Srećan rođendan, {name}!")
        time_text = text("birthday_time", "").strip()
        match = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", time_text)
        self.birthday_time = f"{int(match[1]):02d}:{match[2]}" if match else "09:00"
        try:
            self.tz = ZoneInfo(text("timezone", "") or "Europe/Belgrade")
        except Exception:
            self.tz = ZoneInfo("Europe/Belgrade")


class RoomRules:
    """{room_id: {rule: bool}} in a JSON file."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.rooms: dict[str, dict[str, bool]] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for room_id, rules in (data.get("rooms") or {}).items():
                self.rooms[room_id] = {r: bool(rules.get(r)) for r in RULES}

    def save(self) -> None:
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"rooms": self.rooms}, handle, indent=1)
        os.replace(temporary, self.path)

    def get(self, room_id: str) -> dict[str, bool]:
        with self.lock:
            return dict(self.rooms.get(room_id) or {r: False for r in RULES})

    def on(self, room_id: str, rule: str) -> bool:
        with self.lock:
            return bool((self.rooms.get(room_id) or {}).get(rule))

    def active(self, room_id: str) -> bool:
        with self.lock:
            return any((self.rooms.get(room_id) or {}).values())

    def active_rooms(self) -> list[str]:
        with self.lock:
            return sorted(r for r, rules in self.rooms.items() if any(rules.values()))

    def set(self, room_id: str, rules: dict) -> dict[str, bool]:
        clean = {r: bool(rules.get(r)) for r in RULES}
        with self.lock:
            if any(clean.values()):
                self.rooms[room_id] = clean
            else:
                self.rooms.pop(room_id, None)
            self.save()
        return clean
