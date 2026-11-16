"""Birthdays people entered themselves: a day and a month, never a year.

Kept in a small JSON file as {user_id: [month, day]}. The bot congratulates
in the groups where the owner switched the birthdays rule on.
"""
from __future__ import annotations

import calendar
import json
import os
import threading
from datetime import date


def valid(month, day) -> bool:
    """Whether [month] and [day] name a day of the year. 29 February counts."""
    if isinstance(month, bool) or isinstance(day, bool):
        return False
    if not isinstance(month, int) or not isinstance(day, int):
        return False
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2024, month)[1]


class Birthdays:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.days: dict[str, tuple[int, int]] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for user, value in (data.get("birthdays") or {}).items():
                if isinstance(value, list) and len(value) == 2 and valid(*value):
                    self.days[user] = (value[0], value[1])

    def _save(self) -> None:
        temporary = f"{self.path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"birthdays": {u: list(d) for u, d in self.days.items()}}, handle, indent=1)
        os.replace(temporary, self.path)

    def get(self, user: str) -> tuple[int, int] | None:
        with self.lock:
            return self.days.get(user)

    def set(self, user: str, month: int, day: int) -> None:
        with self.lock:
            self.days[user] = (month, day)
            self._save()

    def remove(self, user: str) -> None:
        with self.lock:
            if self.days.pop(user, None) is not None:
                self._save()

    def on(self, today: date) -> list[str]:
        """Who has their birthday [today]. Someone born on 29 February is
        congratulated on 28 February in years without one."""
        wanted = {(today.month, today.day)}
        if (today.month, today.day) == (2, 28) and not calendar.isleap(today.year):
            wanted.add((2, 29))
        with self.lock:
            return sorted(u for u, d in self.days.items() if d in wanted)
