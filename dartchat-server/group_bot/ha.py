"""Publish each group's numbers as Home Assistant sensors.

Only works inside the add-on, where the Supervisor provides SUPERVISOR_TOKEN.
Sensors are named after the group, for example
sensor.dartchat_drustvo_poruke_danas. They reappear on the next update after
Home Assistant restarts.
"""
from __future__ import annotations

import logging
import os

import aiohttp

log = logging.getLogger("group_bot.ha")

API = os.environ.get("DARTCHAT_HA_API", "http://supervisor/core/api/states")


class HASensors:
    def __init__(self, http: aiohttp.ClientSession, bot, room_name):
        """[room_name] is a function room_id -> the group's name."""
        self.http = http
        self.bot = bot
        self.room_name = room_name
        self.token = os.environ.get("SUPERVISOR_TOKEN")

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def build(self, room_id: str, day: str) -> dict[str, dict]:
        from .core import slug
        stats, store = self.bot.stats, self.bot.store
        name = self.room_name(room_id) or "grupa"
        prefix = "sensor.dartchat_" + (slug(name) or "grupa")
        s = stats.day_stats(room_id, day)
        t = stats.top_stats(room_id, day, limit=5)
        all_time = store.top(room_id, limit=5)
        top_name, top_count = t.posters[0] if t.posters else ("-", 0)
        rep_name, rep_count = all_time[0] if all_time else ("-", 0)
        sensors = {
            f"{prefix}_poruke_danas": {
                "state": s.messages,
                "attributes": {"icon": "mdi:message-text",
                               "friendly_name": f"{name} poruke danas",
                               "unit_of_measurement": "poruka", "state_class": "total",
                               "pisalo": s.people, "linkova": s.links,
                               "ponovljenih": s.reposts, "odgovora": s.replies,
                               "najaktivniji_sat": s.busiest_hour},
            },
            f"{prefix}_poruke_ukupno": {
                "state": s.total_messages,
                "attributes": {"icon": "mdi:message-text",
                               "friendly_name": f"{name} poruke ukupno",
                               "unit_of_measurement": "poruka",
                               "state_class": "total_increasing"},
            },
            f"{prefix}_reposti_danas": {
                "state": s.reposts,
                "attributes": {"icon": "mdi:repeat", "friendly_name": f"{name} reposti danas",
                               "unit_of_measurement": "reposta"},
            },
            f"{prefix}_najaktivniji_danas": {
                "state": top_name,
                "attributes": {"icon": "mdi:account-voice",
                               "friendly_name": f"{name} najaktivniji danas",
                               "poruka": top_count, "lista": {n: c for n, c in t.posters}},
            },
            f"{prefix}_ponavljac": {
                "state": rep_name,
                "attributes": {"icon": "mdi:emoticon-cool",
                               "friendly_name": f"{name} ponavljač (ukupno)",
                               "reposta": rep_count, "lista": {n: c for n, c in all_time}},
            },
        }
        label = self.bot.keyword_label(room_id)
        if label:
            kw = store.top(room_id, limit=5, table="keyword")
            sensors[f"{prefix}_keyword"] = {
                "state": kw[0][0] if kw else "-",
                "attributes": {"icon": "mdi:microphone-message",
                               "friendly_name": f"{name} {label} (ukupno)",
                               "danas": s.keyword, "lista": {n: c for n, c in kw}},
            }
        return sensors

    async def update(self, rooms: list[str], day: str) -> None:
        if not self.enabled:
            return
        headers = {"Authorization": f"Bearer {self.token}"}
        for room_id in rooms:
            for entity, body in self.build(room_id, day).items():
                try:
                    async with self.http.post(f"{API}/{entity}", json=body, headers=headers,
                                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status >= 300:
                            log.warning("sensor %s: HTTP %s", entity, r.status)
                except Exception as e:
                    log.warning("could not update sensor %s: %s", entity, e)
                    return
