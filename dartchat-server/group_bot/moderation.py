"""The owner's ban and mute commands, for example "!ban @marko 1d".

Only the owner's commands count. The bot does not ban anyone itself: it
passes the request to the Dart Chat service on this machine, which checks
again that the owner asked, applies it and lifts it when the time is up.
"""
from __future__ import annotations

import re
from datetime import datetime

COMMANDS = {"!ban": ("ban", True), "!mute": ("mute", True),
            "!unban": ("ban", False), "!unmute": ("mute", False)}
UNITS = {"m": 1, "min": 1, "h": 60, "s": 60, "d": 24 * 60, "w": 7 * 24 * 60,
         "n": 7 * 24 * 60}
MAX_MINUTES = 366 * 24 * 60
HELP = "Napiši npr. !ban @marko 1d ili !mute @marko 2h (m, h, d, w)."


def parse_duration(text: str) -> int | None:
    """Minutes for "30m", "2h", "1d" or "1w"; None when it is not a time."""
    match = re.fullmatch(r"(\d{1,4})\s*([a-z]+)", text.strip().lower())
    if not match or match[2] not in UNITS:
        return None
    minutes = int(match[1]) * UNITS[match[2]]
    return minutes if 1 <= minutes <= MAX_MINUTES else None


def parse_command(body: str):
    """(kind, start, name, minutes) for a ban or mute command, "help" for one
    that is not complete, or None for any other message."""
    words = body.strip().split()
    if not words or words[0].lower() not in COMMANDS:
        return None
    kind, start = COMMANDS[words[0].lower()]
    if start:
        if len(words) < 3:
            return "help"
        minutes = parse_duration(words[-1])
        name = " ".join(words[1:-1])
        if minutes is None:
            return "help"
    else:
        if len(words) < 2:
            return "help"
        minutes, name = None, " ".join(words[1:])
    name = name.lstrip("@").strip()
    return (kind, start, name, minutes) if name else "help"


def find_person(name: str, people: dict[str, str]) -> str | None:
    """The user id that [name] means among [people] (user id -> shown name):
    a full user id, a username, or the name shown in the group. None when
    nobody or more than one person matches."""
    wanted = name.strip().lstrip("@").lower()
    if not wanted:
        return None
    matches = set()
    for user_id, shown in people.items():
        localpart = user_id[1:].split(":")[0].lower()
        if wanted in (user_id.lower().lstrip("@"), localpart, (shown or "").strip().lower()):
            matches.add(user_id)
    return matches.pop() if len(matches) == 1 else None


def when_text(until_ms: int, zone) -> str:
    local = datetime.fromtimestamp(until_ms / 1000, tz=zone)
    return f"{local.day}.{local.month}. u {local:%H:%M}"


def done_text(kind: str, start: bool, name: str, until_ms: int | None, zone) -> str:
    if kind == "mute":
        if start:
            return f"🔇 {name} ne može da piše u ovoj grupi do {when_text(until_ms, zone)}."
        return f"🔊 {name} ponovo može da piše."
    if start:
        return f"⛔ {name} je izbačen iz grupe do {when_text(until_ms, zone)}."
    return f"✅ {name} je vraćen u grupu."
