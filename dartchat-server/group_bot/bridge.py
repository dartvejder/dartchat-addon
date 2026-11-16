"""A bridge between Dart Chat groups and one Signal group.

The owner's Signal bot runs as another Home Assistant add-on. It talks to
this bot over Home Assistant's internal network, on port 6170, with a shared
password (the `signal_bridge_token` option on both add-ons):

  GET  /_dartchat/v1/bridge/outbox?after=N&wait=25
       Messages from Dart Chat groups with the "Bridge to Signal" rule on,
       newer than N. Waits up to `wait` seconds for one to arrive.
  POST /_dartchat/v1/bridge/inbox
       A message from the Signal group, posted by the bot into every Dart
       Chat group with the rule on:
       {"name": "Marko", "text": "...", "quote": {"name": ..., "text": ...},
        "images": [{"data": base64, "mime": "image/jpeg", "filename": ...,
                    "width": 800, "height": 600}]}
  GET  /_dartchat/v1/bridge/status
       {"on": true} while at least one group has the rule on.

Switching the rule off in the app stops both directions at once: nothing is
queued for Signal, and messages from Signal are dropped.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import re
import time
from collections import deque

PREFIX = "/_dartchat/v1/bridge"
PORT = 6170
MAX_ITEMS = 200
MAX_BYTES = 80 * 1024 * 1024  # pictures waiting while the Signal bot is down
MAX_WAIT = 50


def snippet(text: str, length: int = 80) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= length else text[:length - 1] + "…"


def looks_like_filename(body: str) -> bool:
    return bool(re.fullmatch(r"[^\s/\\]{1,120}\.[A-Za-z0-9]{2,5}", body.strip()))


def signal_text(name: str, text: str, quote: dict | None = None) -> str:
    """How a message from the other side reads: "Marko: text", with the
    message it answers above it."""
    lines = []
    if quote and (quote.get("text") or quote.get("name")):
        who = quote.get("name") or ""
        lines.append(f"↪ {who + ': ' if who else ''}{snippet(quote.get('text') or '📷')}")
    lines.append(f"{name}: {text}" if text else f"{name}:")
    return "\n".join(lines)


class Outbox:
    """Messages from Dart Chat waiting for the Signal bot to pick them up."""

    def __init__(self):
        self.items: deque[dict] = deque()
        self.ids = itertools.count(1)
        self.last = 0
        self.changed = asyncio.Event()

    def size(self) -> int:
        return sum(len((i.get("image") or {}).get("data", "")) for i in self.items)

    def add(self, name: str, text: str, image: dict | None = None,
            quote: dict | None = None) -> dict:
        self.last = next(self.ids)
        item = {"id": self.last, "name": name, "text": text, "time": int(time.time())}
        if image:
            item["image"] = image
        if quote:
            item["quote"] = quote
        self.items.append(item)
        while len(self.items) > MAX_ITEMS or (len(self.items) > 1 and self.size() > MAX_BYTES):
            self.items.popleft()
        self.changed.set()
        self.changed = asyncio.Event()
        return item

    def after(self, cursor: int) -> list[dict]:
        if cursor > self.last:
            cursor = 0  # this bot restarted since the Signal bot last asked
        while self.items and self.items[0]["id"] <= cursor:
            self.items.popleft()  # picked up: the Signal bot asks for newer ones
        return list(self.items)

    async def wait(self, cursor: int, seconds: float) -> list[dict]:
        items = self.after(cursor)
        if items or seconds <= 0:
            return items
        changed = self.changed
        try:
            await asyncio.wait_for(changed.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return self.after(cursor)

    def clear(self) -> None:
        self.items.clear()


def image_item(data: bytes, mime: str, filename: str) -> dict:
    return {"data": base64.b64encode(data).decode("ascii"), "mime": mime or "image/jpeg",
            "filename": filename or "photo.jpg"}


def image_size(data: bytes) -> tuple[int, int] | None:
    """Width and height of a JPEG, PNG, GIF or WebP picture, if easy to tell."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if kind == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                length = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + length
    except (IndexError, ValueError):
        pass
    return None
