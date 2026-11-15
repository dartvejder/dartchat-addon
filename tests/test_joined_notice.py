"""The "someone you invited has joined" notice."""

import time

import invite_service
from push_gateway import fcm_message, wake_data


def test_the_wake_up_says_joined_and_names_nobody():
    data = wake_data({"kind": "joined", "counts": {"unread": 1}, "user": "@x:y"})
    assert data == {"unread": "1", "kind": "joined"}


def test_other_kinds_are_not_passed_on():
    assert "kind" not in wake_data({"kind": "anything", "counts": {}})


def test_iphone_shows_the_joined_text():
    data = {"unread": "1", "kind": "joined"}
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")["message"]
    aps = message["apns"]["payload"]["aps"]
    assert aps["alert"]["body"] == "Someone you invited has joined"
    assert message["apns"]["headers"]["apns-collapse-id"] == "dartchat-joined"


def _use_store(tmp_path, monkeypatch):
    store = invite_service.Store(str(tmp_path / "invites.json"))
    monkeypatch.setattr(invite_service, "store", store)
    return store


def test_owner_phones_are_remembered_and_notified(tmp_path, monkeypatch):
    store = _use_store(tmp_path, monkeypatch)
    pushers = {
        "pushers": [
            {"kind": "http", "pushkey": "phone-1", "app_id": "app.android"},
            {"kind": "email", "pushkey": "mail"},
            {"kind": "http", "pushkey": ""},
        ]
    }
    monkeypatch.setattr(invite_service, "chat", lambda *a, **k: (200, pushers))
    invite_service.remember_phones("@boss:x", "token")
    assert store.data["owner_phones"] == {
        "@boss:x": [{"pushkey": "phone-1", "app_id": "app.android"}]
    }

    sent = []
    monkeypatch.setattr(
        invite_service,
        "http_json",
        lambda method, url, body=None, **k: sent.append((method, url, body))
        or (200, {}),
    )
    invite_service.notify_joined("@boss:x")
    for _ in range(50):
        if sent:
            break
        time.sleep(0.02)
    assert sent == [
        (
            "POST",
            invite_service.PUSH_URL,
            {
                "notification": {
                    "kind": "joined",
                    "counts": {"unread": 1},
                    "devices": [{"pushkey": "phone-1", "app_id": "app.android"}],
                }
            },
        )
    ]


def test_nothing_is_sent_when_no_phone_is_known(tmp_path, monkeypatch):
    _use_store(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(
        invite_service, "http_json", lambda *a, **k: sent.append(a) or (200, {})
    )
    invite_service.notify_joined("@boss:x")
    time.sleep(0.1)
    assert sent == []


# ---- "someone changed their name" -------------------------------------------


def test_the_renamed_wake_up_names_nobody():
    data = wake_data({"kind": "renamed", "counts": {"unread": 1}, "user": "@x:y"})
    assert data == {"unread": "1", "kind": "renamed"}
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")["message"]
    assert message["apns"]["payload"]["aps"]["alert"]["body"] == (
        "Someone changed their name"
    )
    assert message["apns"]["headers"]["apns-collapse-id"] == "dartchat-renamed"


def _renames(tmp_path, monkeypatch):
    store = _use_store(tmp_path, monkeypatch)
    store.data["owners"] = ["@boss:x"]
    store.data["owner_phones"] = {"@boss:x": [{"pushkey": "p", "app_id": "a"}]}
    sent = []
    monkeypatch.setattr(
        invite_service,
        "http_json",
        lambda method, url, body=None, **k: sent.append(body) or (200, {}),
    )
    return store, sent


def _wait(sent):
    for _ in range(50):
        if sent:
            break
        time.sleep(0.02)


def test_a_changed_name_is_remembered_and_the_owner_is_told(tmp_path, monkeypatch):
    store, sent = _renames(tmp_path, monkeypatch)
    invite_service.note_rename("@ana:x", "Anka", " Anchi ")
    _wait(sent)
    assert store.data["renames"]["@ana:x"]["from"] == "Anka"
    assert [body["notification"]["kind"] for body in sent] == ["renamed"]
    # The notice itself carries neither name.
    assert "Anka" not in str(sent) and "Anchi" not in str(sent)


def test_first_names_same_names_and_the_owner_are_not_news(tmp_path, monkeypatch):
    store, sent = _renames(tmp_path, monkeypatch)
    invite_service.note_rename("@ana:x", "", "Ana")
    invite_service.note_rename("@ana:x", "Ana", "Ana ")
    invite_service.note_rename("@ana:x", "ana", "Ana Banana")
    invite_service.note_rename("@boss:x", "Boss", "Big boss")
    time.sleep(0.1)
    assert sent == [] and "renames" not in store.data
