"""Tests for muted chats that the server unmutes on time."""

import pytest

import invite_service as service

ROOM = "!friends:chat.test"


class Handler:
    pass


def as_user(user, token="t"):
    handler = Handler()
    handler.user = user
    handler.token = token
    return handler


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, handler.token))
    monkeypatch.setattr(service, "QUIET_FILE", str(tmp_path / "quiet.json"))
    monkeypatch.setattr(service, "quiet", None)
    monkeypatch.setattr(service, "now_ms", lambda: 1_000_000)
    calls = []

    def fake_chat(method, path, body=None, token=None):
        calls.append((method, path, token))
        return fake_chat.answer

    fake_chat.answer = (200, {})
    monkeypatch.setattr(service, "chat", fake_chat)
    return fake_chat, calls


def test_remembers_and_forgets_when_to_unmute():
    ana = as_user("@ana:chat.test")
    assert service.set_quiet(ana, {"room_id": ROOM, "until": 2_000_000}) == {
        "room_id": ROOM,
        "until": 2_000_000,
    }
    # A new time replaces the old one.
    service.set_quiet(ana, {"room_id": ROOM, "until": 3_000_000})
    assert service.list_quiet(ana, {}) == {"muted": [{"room_id": ROOM, "until": 3_000_000}]}
    assert service.list_quiet(as_user("@marko:chat.test"), {}) == {"muted": []}
    # Muted for good, or unmuted early: nothing to do any more.
    service.set_quiet(ana, {"room_id": ROOM, "until": None})
    assert service.list_quiet(ana, {}) == {"muted": []}


def test_refuses_bad_requests():
    ana = as_user("@ana:chat.test")
    for body in (
        {"until": 2_000_000},
        {"room_id": "friends", "until": 2_000_000},
        {"room_id": ROOM, "until": "soon"},
        {"room_id": ROOM, "until": True},
        {"room_id": ROOM, "until": 1_000_000 + service.QUIET_MAX_AHEAD_MS + 1},
    ):
        with pytest.raises(service.ApiError):
            service.set_quiet(ana, body)


def test_unmutes_on_time_with_the_sign_in_that_asked(fresh):
    _, calls = fresh
    service.set_quiet(as_user("@ana:chat.test", "ana-token"), {"room_id": ROOM, "until": 2_000_000})
    assert service.unmute_due(now=1_999_999) == 0
    assert calls == []
    assert service.unmute_due(now=2_000_000) == 1
    assert calls == [
        ("DELETE", "/pushrules/global/override/%21friends%3Achat.test", "ana-token")
    ]
    assert service.list_quiet(as_user("@ana:chat.test"), {}) == {"muted": []}


def test_already_unmuted_counts_as_done(fresh):
    fake_chat, _ = fresh
    fake_chat.answer = (404, {"errcode": "M_NOT_FOUND"})
    service.set_quiet(as_user("@ana:chat.test"), {"room_id": ROOM, "until": 2_000_000})
    assert service.unmute_due(now=2_000_000) == 1
    assert service.load_quiet() == []


def test_signed_out_uses_the_helper_and_keeps_trying(fresh, monkeypatch):
    fake_chat, _ = fresh
    fake_chat.answer = (401, {"errcode": "M_UNKNOWN_TOKEN"})
    helper_calls = []

    def fake_http(method, url, body=None, headers=None, timeout=20):
        helper_calls.append((method, url, headers))
        return 200, {}

    monkeypatch.setattr(service, "_load_link_appservice", lambda: {"as_token": "as"})
    monkeypatch.setattr(service, "http_json", fake_http)
    service.set_quiet(as_user("@ana:chat.test"), {"room_id": ROOM, "until": 2_000_000})
    assert service.unmute_due(now=2_000_000) == 1
    method, url, headers = helper_calls[0]
    assert method == "DELETE"
    assert url.endswith("/override/%21friends%3Achat.test?user_id=%40ana%3Achat.test")
    assert headers == {"Authorization": "Bearer as"}


def test_gives_up_after_a_day(fresh, monkeypatch):
    fake_chat, _ = fresh
    fake_chat.answer = (500, {})
    monkeypatch.setattr(service, "_load_link_appservice", lambda: None)
    service.set_quiet(as_user("@ana:chat.test"), {"room_id": ROOM, "until": 2_000_000})
    assert service.unmute_due(now=2_000_000) == 0
    assert len(service.load_quiet()) == 1  # tried again later
    assert service.unmute_due(now=2_000_001 + service.QUIET_GIVE_UP_SECONDS * 1000) == 0
    assert service.load_quiet() == []
