"""Unit tests for what the push gateway asks Firebase to deliver."""

from push_gateway import fcm_message, wake_data

DATA = {"event_id": "$e", "room_id": "!r", "unread": "3"}


def test_android_gets_a_silent_high_priority_wake_up():
    message = fcm_message("tok", DATA, "com.dartvejder.dartchat.android")["message"]
    assert message == {"token": "tok", "data": DATA, "android": {"priority": "high"}}


def test_unknown_app_is_treated_like_android():
    assert "android" in fcm_message("tok", DATA)["message"]


def test_iphone_gets_a_fixed_notice_with_the_unread_count():
    message = fcm_message("tok", DATA, "com.dartvejder.dartchat.ios")["message"]
    assert "android" not in message
    aps = message["apns"]["payload"]["aps"]
    assert aps["alert"] == {"title": "Dart Chat", "body": "New message"}
    assert aps["badge"] == 3
    assert message["apns"]["headers"]["apns-push-type"] == "alert"


def test_iphone_all_read_clears_the_counter_quietly():
    data = dict(DATA, unread="0")
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")["message"]
    aps = message["apns"]["payload"]["aps"]
    assert aps == {"badge": 0, "content-available": 1}
    assert message["apns"]["headers"]["apns-push-type"] == "background"


def test_nothing_private_leaves_the_server():
    data = wake_data(
        {
            "event_id": "$e",
            "room_id": "!r",
            "sender": "@a:x",
            "room_name": "Secret",
            "content": {"body": "hi"},
            "counts": {"unread": 2},
        }
    )
    assert data == {"event_id": "$e", "room_id": "!r", "unread": "2"}
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")
    text = str(message)
    for private in ("@a:x", "Secret", "hi"):
        assert private not in text
