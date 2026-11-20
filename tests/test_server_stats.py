"""Tests for the owner's server stats screen."""

import json
from datetime import date

import pytest

import invite_service as service


class Handler:
    headers = {}


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "COUNTS_FILE", str(tmp_path / "counts.json"))
    monkeypatch.setattr(service, "ACTIVITY_FILE", str(tmp_path / "activity.json"))
    monkeypatch.setattr(service, "activity", {"seen": None, "saved": {}})
    monkeypatch.setattr(service, "room_kinds", {})
    monkeypatch.setattr(service, "recent_events", [])
    monkeypatch.setattr(service, "SERVER_NAME", "chat.test")
    monkeypatch.setattr(service, "load_hidden_presence", lambda: set())
    return tmp_path


def test_counts_per_person_without_touching_group_counts(monkeypatch):
    monkeypatch.setattr(service, "room_kind", lambda room, token: (True, "Ekipa"))
    service.count_message("@ana:chat.test", "t", "!g", "m.room.encrypted", {}, '{"event_id":"$1"}')
    service.count_message("@ana:chat.test", "t", "!g", "m.reaction", {}, '{"event_id":"$2"}')
    data = service.load_counts()
    day = next(iter(data["days"].values()))
    assert day["people"] == {"@ana:chat.test": 1}
    assert day["groups"] == {"!g": 1}
    assert day["reactions"] == 1


def test_note_seen_saves_at_most_every_few_minutes(files):
    service.note_seen("@ana:chat.test", now=1000)
    service.note_seen("@ana:chat.test", now=1100)
    saved = json.loads((files / "activity.json").read_text())
    assert saved["seen"]["@ana:chat.test"] == 1000
    assert service.seen_times()["@ana:chat.test"] == 1100
    service.note_seen("@ana:chat.test", now=1000 + service.SEEN_SAVE_SECONDS)
    saved = json.loads((files / "activity.json").read_text())
    assert saved["seen"]["@ana:chat.test"] == 1000 + service.SEEN_SAVE_SECONDS


def test_bot_is_not_noted():
    service.note_seen(service.BOT_USER, now=5)
    assert service.BOT_USER not in service.seen_times()


def test_message_numbers_windows():
    today = date(2026, 11, 20)
    data = {
        "rooms": {"!g": "Ekipa"},
        "days": {
            "2026-11-20": {"private": 2, "groups": {"!g": 3}, "people": {"@a:x": 5}},
            "2026-11-19": {"private": 1, "reactions": 4, "people": {"@a:x": 1}},
            "2026-11-01": {"groups": {"!g": 10}, "people": {"@b:x": 10}},
            "2026-01-01": {"private": 100},
        },
    }
    totals, daily, groups, people, since = service.message_numbers(data, today)
    assert totals["today"] == 5
    assert totals["yesterday"] == 1
    assert totals["week"] == 6
    assert totals["month"] == 16
    assert totals["all"] == 116
    assert totals["reactions_month"] == 4
    assert len(daily) == 14 and daily[-1] == {"day": "2026-11-20", "messages": 5}
    assert groups == [{"name": "Ekipa", "month": 13}]
    assert people["@a:x"] == {"today": 5, "week": 6, "month": 6}
    assert people["@b:x"] == {"today": 0, "week": 0, "month": 10}
    assert since == "2026-01-01"


def test_stats_owner_only(monkeypatch):
    def refuse(handler):
        raise service.ApiError(403, "M_FORBIDDEN", "Only the server owner can do this.")

    monkeypatch.setattr(service, "require_admin", refuse)
    with pytest.raises(service.ApiError) as error:
        service.server_stats(Handler(), {})
    assert error.value.status == 403


def test_stats_people_activity_and_hidden(monkeypatch):
    now = service.time.time()
    monkeypatch.setattr(service, "require_admin", lambda h: ("@dottore:chat.test", "t"))
    monkeypatch.setattr(service, "all_accounts", lambda token=None: [
        "@dottore:chat.test", "@ana:chat.test", "@sky:chat.test", "@old:chat.test",
        service.BOT_USER,
    ])
    monkeypatch.setattr(service, "presence_hidden", lambda u: u == "@sky:chat.test")

    class Store:
        lock = service.threading.Lock()
        data = {"deactivated": ["@old:chat.test"]}

    monkeypatch.setattr(service, "store", Store())
    monkeypatch.setattr(service, "storage_numbers", lambda: {"used": 1})

    def chat(method, path, body=None, token=None):
        if "ana" in path:
            return 200, {"presence": "offline", "last_active_ago": 3 * 3600 * 1000}
        if "dottore" in path:
            return 200, {"presence": "online", "currently_active": True}
        return 200, {"presence": "offline", "last_active_ago": 1000}

    monkeypatch.setattr(service, "chat", chat)
    answer = service.server_stats(Handler(), {})
    names = [p["user_id"] for p in answer["people"]]
    assert names == ["@dottore:chat.test", "@ana:chat.test", "@sky:chat.test"]
    ana = answer["people"][1]
    assert abs(ana["last_seen"] / 1000 - (now - 3 * 3600)) < 5
    sky = answer["people"][2]
    assert sky["hidden"] is True and sky["last_seen"] is None
    assert answer["active"] == {"now": 1, "today": 2, "week": 2}
    assert answer["accounts"] == 3
    assert answer["storage"] == {"used": 1}


def test_storage_numbers_uses_storage_report(monkeypatch, tmp_path):
    import storage_report

    monkeypatch.setattr(service, "stats_cache", {"until": 0, "storage": None})
    monkeypatch.setattr(storage_report, "measure", lambda: {
        "media": 300, "database": 200, "other": 100, "free": 1000, "disk": 2000,
    })
    monkeypatch.setattr(storage_report, "load_history", lambda: {
        "2026-10-01": {"media": 100, "database": 100, "other": 100, "free": 0, "disk": 0},
    })
    answer = service.storage_numbers()
    assert answer["used"] == 600
    assert answer["grew_month"] == 300
    assert answer["free"] == 1000
