"""The daily storage email and the message counts behind it."""

import json
from datetime import date, datetime, timedelta

import invite_service
import storage_report as sr


def entry(media, database=0, other=0, free=10**12, disk=2 * 10**12):
    return {"media": media, "database": database, "other": other,
            "free": free, "disk": disk}


def test_measure_splits_media_database_and_other(tmp_path, monkeypatch):
    (tmp_path / "db" / "media").mkdir(parents=True)
    (tmp_path / "db" / "media" / "photo").write_bytes(b"x" * 10000)
    (tmp_path / "db" / "000001.sst").write_bytes(b"x" * 5000)
    (tmp_path / "invites.json").write_bytes(b"x" * 3000)
    monkeypatch.setattr(sr, "DATA", str(tmp_path))
    monkeypatch.setattr(sr, "DB", str(tmp_path / "db"))
    monkeypatch.setattr(sr, "MEDIA", str(tmp_path / "db" / "media"))
    m = sr.measure()
    assert m["media"] >= 10000 and m["database"] >= 5000 and m["other"] >= 3000
    assert m["media"] < 20000 and m["database"] < 10000
    assert m["disk"] > 0 and m["free"] > 0


def test_first_start_measures_at_once_and_then_daily(monkeypatch):
    monkeypatch.setattr(sr, "measure", lambda: entry(1))
    h, changed, due = sr.run_once(datetime(2026, 11, 15, 3, 0), {}, (8, 0))
    assert changed and not due and "2026-11-15" in h
    h, changed, due = sr.run_once(datetime(2026, 11, 16, 7, 59), h, (8, 0))
    assert not changed and "2026-11-16" not in h
    h, changed, due = sr.run_once(datetime(2026, 11, 16, 8, 0), h, (8, 0))
    assert changed and due and "2026-11-16" in h


def test_report_shows_growth_and_messages():
    today = date(2026, 11, 15)
    history = {
        (today - timedelta(days=d)).isoformat(): entry(10**9 - d * 10**6)
        for d in range(0, 40)
    }
    counts = {
        "days": {
            "2026-11-14": {"groups": {"!a:x": 5, "!b:x": 2}, "private": 7, "reactions": 3},
            "2026-11-15": {"groups": {"!a:x": 1}, "private": 1},
            "2026-10-01": {"private": 100},
        },
        "rooms": {"!a:x": "Ekipa"},
    }
    subject, text, html = sr.build_report(history, today, counts)
    assert "used" in subject and "free" in subject
    assert "Last day: +976.6 KB" in text
    assert "Ekipa: 5 / 6 / 6 / 6" in text
    assert "Group without a name: 2 / 2 / 2 / 2" in text
    assert "Private chats (all together): 7 / 8 / 108 / 108" in text
    assert "All messages: 14 / 16 / 116 / 116" in text
    assert "Reactions: 3 / 3 / 3 / 3" in text
    assert "October 2026" in text and "2026 (so far)" in text
    assert "Ekipa" in html and "<table" in html


def test_report_on_the_first_day_has_no_growth_yet():
    today = date(2026, 11, 15)
    _, text, _ = sr.build_report({today.isoformat(): entry(5000)}, today, None)
    assert "measuring starts today" in text
    assert "Messages (" not in text


def test_old_measurements_are_thinned_to_monthly():
    today = date(2026, 11, 15)
    history = {(today - timedelta(days=d)).isoformat(): entry(d) for d in range(800)}
    kept = sr.thin(history, today)
    old = [d for d in kept if d < (today - timedelta(days=sr.KEEP_DAILY_DAYS)).isoformat()]
    assert len(old) == len({d[:7] for d in old}) and len(old) < 20


def test_message_kinds():
    assert invite_service.message_kind("m.room.encrypted", {"ciphertext": "x"}) == "message"
    assert invite_service.message_kind("m.reaction", {}) == "reaction"
    edit = {"m.relates_to": {"rel_type": "m.replace", "event_id": "$e"}}
    assert invite_service.message_kind("m.room.encrypted", edit) is None
    reply = {"m.relates_to": {"m.in_reply_to": {"event_id": "$e"}}}
    assert invite_service.message_kind("m.room.encrypted", reply) == "message"
    assert invite_service.message_kind("m.call.invite", {}) is None


def test_counting_groups_private_and_repeats(tmp_path, monkeypatch):
    path = tmp_path / "counts.json"
    monkeypatch.setattr(invite_service, "COUNTS_FILE", str(path))
    monkeypatch.setattr(invite_service, "room_kinds", {})
    monkeypatch.setattr(invite_service, "recent_events", [])

    def fake_chat(method, p, body=None, token=None):
        if p.endswith("m.room.name"):
            return (200, {"name": "Ekipa"}) if "group" in p else (404, {})
        return 200, {"joined": {"@a:x": {}, "@b:x": {}}}

    monkeypatch.setattr(invite_service, "chat", fake_chat)
    msg = {"ciphertext": "x"}
    invite_service.count_message("@a:x", "t", "!group:x", "m.room.encrypted", msg, b'{"event_id":"$1"}')
    invite_service.count_message("@a:x", "t", "!group:x", "m.room.encrypted", msg, b'{"event_id":"$1"}')
    invite_service.count_message("@a:x", "t", "!dm:x", "m.room.encrypted", msg, b'{"event_id":"$2"}')
    invite_service.count_message("@a:x", "t", "!dm:x", "m.reaction", {}, b'{"event_id":"$3"}')
    invite_service.count_message(invite_service.BOT_USER, "t", "!group:x", "m.room.message", msg, b'{"event_id":"$4"}')
    data = json.loads(path.read_text())
    (day,) = data["days"].values()
    assert day == {"groups": {"!group:x": 1}, "private": 1, "reactions": 1}
    assert data["rooms"] == {"!group:x": "Ekipa"}
