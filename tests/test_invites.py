"""Invitations: devices, channels, resending, expiry, Play tester list."""

import pytest

import invite_service as svc


class FakeHandler:
    headers = {}

    def caller_address(self):
        return "1.2.3.4"


@pytest.fixture
def owner(tmp_path, monkeypatch):
    store = svc.Store(str(tmp_path / "invites.json"))
    monkeypatch.setattr(svc, "store", store)
    monkeypatch.setattr(svc, "require_admin", lambda handler: ("@boss:x", "tok"))
    monkeypatch.setattr(svc, "display_name", lambda user, token: "Boss")
    monkeypatch.setattr(svc, "APP_LINK", "https://play.example/test")
    monkeypatch.setattr(svc, "IPHONE_LINK", "https://testflight.example/join")
    monkeypatch.setattr(svc, "WEB_LINK", "https://web.example")
    monkeypatch.setattr(svc, "RESEND_KEY", "key")
    sent = []

    def fake_http(method, url, body=None, headers=None, timeout=20):
        sent.append((method, url, body))
        if method == "POST":
            return 200, {"id": f"mail-{len(sent)}"}
        return 200, {"last_event": "bounced"}

    monkeypatch.setattr(svc, "http_json", fake_http)
    return store, sent


def test_email_lists_every_device_when_none_is_picked(owner):
    text, html = svc.invite_email("ABCD2345", "Boss", [])
    for link in (
        "https://play.example/test",
        "https://testflight.example/join",
        "https://web.example/?invite=ABCD2345",
        "https://web.example/windows/",
        "https://web.example/linux/",
        "https://web.example/join.html#ABCD-2345",
    ):
        assert link in text and link in html
    assert "TestFlight from the App Store" in text


def test_email_for_an_iphone_has_no_play_link(owner):
    text, html = svc.invite_email("ABCD2345", "Boss", ["iphone"])
    assert "testflight.example" in html
    assert "play.example" not in html and "play.example" not in text
    # The computer versions are always mentioned on the side.
    assert "Also on a computer" in text and "https://web.example/windows/" in html
    assert "on a computer" in html


def test_invite_on_several_channels(owner):
    store, sent = owner
    shown = svc.create_invite(
        FakeHandler(),
        {
            "email": "Pera@Example.com",
            "platforms": ["iphone"],
            "channels": ["qr", "email", "link"],
        },
    )
    assert shown["channels"] == ["email", "link", "qr"]
    assert shown["platforms"] == ["iphone"]
    assert shown["email_sent"] is True
    assert shown["link"] == f"https://web.example/join.html#{shown['code']}"
    assert shown["expires_at"] > shown["created_at"]
    assert len(sent) == 1 and sent[0][2]["to"] == ["pera@example.com"]
    assert store.data["invites"][0]["email_id"] == "mail-1"


def test_link_only_needs_a_name_and_sends_no_email(owner):
    store, sent = owner
    shown = svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["link"]})
    assert shown["email"] == "" and shown["name"] == "Pera"
    assert sent == []
    with pytest.raises(svc.ApiError):
        svc.create_invite(FakeHandler(), {"channels": ["link"]})
    with pytest.raises(svc.ApiError):
        svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["email"]})
    with pytest.raises(svc.ApiError):
        svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["fax"]})


def test_older_apps_still_send_email(owner):
    store, sent = owner
    shown = svc.create_invite(FakeHandler(), {"email": "a@b.cd"})
    assert shown["channels"] == ["email"] and len(sent) == 1


def test_resend_keeps_a_waiting_code_and_picks_channels_again(owner):
    store, sent = owner
    first = svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["link"]})
    again = svc.resend_invite(
        FakeHandler(), {"channels": ["email", "qr"], "email": "p@x.yz"}, first["id"]
    )
    assert again["code"] == first["code"]
    assert again["channels"] == ["email", "qr"]
    assert again["email"] == "p@x.yz" and again["sends"] == 2
    assert len(sent) == 1
    with pytest.raises(svc.ApiError):
        svc.resend_invite(FakeHandler(), {"channels": []}, first["id"])


def test_expired_invite_is_refused_then_resent_with_a_new_code(owner, monkeypatch):
    store, sent = owner
    first = svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["qr"]})
    store.data["invites"][0]["expires_at"] = 1
    assert svc.public_invite(store.data["invites"][0])["status"] == "expired"
    with pytest.raises(svc.ApiError) as refused:
        svc.register(
            FakeHandler(),
            {"code": first["code"], "username": "pera", "password": "12345678"},
        )
    assert refused.value.errcode == "DARTCHAT_INVITE_EXPIRED"
    again = svc.resend_invite(FakeHandler(), {"channels": ["link"]}, first["id"])
    assert again["status"] == "pending" and again["code"] != first["code"]


def test_old_invites_never_expire(owner):
    store, _ = owner
    store.data["invites"].append(
        {"id": "a" * 16, "email": "o@l.d", "code": "ABCDEFGH", "status": "pending", "created_at": 1}
    )
    shown = svc.public_invite(store.data["invites"][0])
    assert shown["status"] == "pending" and shown["channels"] == ["email"]


def test_bounced_email_shows_in_the_list(owner):
    store, sent = owner
    svc.create_invite(FakeHandler(), {"email": "gone@x.yz"})
    listed = svc.list_invites(FakeHandler(), {})["invites"][0]
    assert listed["email_state"] == "bounced"
    assert ("GET", "https://api.resend.com/emails/mail-1", None) in sent


def test_play_tester_list(owner):
    store, _ = owner
    svc.create_invite(FakeHandler(), {"email": "droid@gmail.com", "platforms": ["android"]})
    assert svc.list_invites(FakeHandler(), {})["invites"][0]["on_play_list"] is False
    assert svc.set_play_tester(FakeHandler(), {"email": "Droid@gmail.com"})["on_play_list"]
    assert svc.list_invites(FakeHandler(), {})["invites"][0]["on_play_list"] is True
    svc.set_play_tester(FakeHandler(), {"email": "droid@gmail.com", "on": False})
    assert store.data["play_testers"] == []


def test_register_names_the_inviter(owner, monkeypatch):
    store, _ = owner
    shown = svc.create_invite(FakeHandler(), {"name": "Pera", "channels": ["link"]})
    monkeypatch.setattr(svc, "chat", lambda *a, **k: (200, {"user_id": "@pera:x"}))
    monkeypatch.setattr(svc, "notify_joined", lambda who: None)
    answer = svc.register(
        FakeHandler(), {"code": shown["code"], "username": "pera", "password": "12345678"}
    )
    assert answer == {"user_id": "@pera:x", "inviter": "@boss:x"}


def test_owner_words_in_the_invitation_email(monkeypatch):
    monkeypatch.setattr(svc, "INVITE_INTRO", "{inviter} te zove na pikado i chat!")
    text, html = svc.invite_email("ABCDEFG", "Dottore", ["android"])
    assert text.startswith("Dottore te zove na pikado i chat!")
    assert "Dottore te zove na pikado i chat!" in html
    assert "A private chat for friends." not in html
    monkeypatch.setattr(svc, "INVITE_INTRO", "")
    text, _ = svc.invite_email("ABCDEFG", "Dottore", ["android"])
    assert text.startswith("Dottore invited you to Dart Chat")
    assert svc.fill_inviter("{inviter} zove", "Ana") == "Ana zove"
    assert svc.fill_inviter("", "Ana") == ""
