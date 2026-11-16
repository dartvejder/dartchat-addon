"""Tests for linking a computer by QR code."""

import json
import threading
import time

import pytest

import invite_service as service

KEY = "A" * 43


class FakeHandler:
    def __init__(self, path="/", token="phone-token"):
        self.path = path
        self.headers = {"Authorization": f"Bearer {token}"}


@pytest.fixture
def server(monkeypatch, tmp_path):
    """The chat server's side: admin commands and appservice logins."""
    calls = {"commands": [], "logins": []}
    registered = {}

    def admin(command):
        calls["commands"].append(command)
        if command.startswith("appservices register"):
            registered["as_token"] = command.split("as_token: ")[1].split("\n")[0]
        return "ok", "@conduit:chat.test"

    def http_json(method, url, body=None, headers=None, timeout=20):
        assert url.endswith("/_matrix/client/v3/login")
        token = headers["Authorization"][7:]
        if token != registered.get("as_token"):
            return 401, {"errcode": "M_UNKNOWN_TOKEN"}
        calls["logins"].append(body)
        return 200, {
            "user_id": body["identifier"]["user"],
            "device_id": "NEWDEVICE",
            "access_token": "computer-token",
        }

    service.links.clear()
    monkeypatch.setattr(service, "LINK_FILE", str(tmp_path / "link.json"))
    monkeypatch.setattr(service, "SERVER_NAME", "chat.test")
    monkeypatch.setattr(service, "_owner_admin_command", admin)
    monkeypatch.setattr(service, "http_json", http_json)
    monkeypatch.setattr(service, "caller", lambda handler: ("@ana:chat.test", "phone-token"))
    return calls


def test_a_phone_links_a_computer(server):
    started = service.start_link(FakeHandler(), {"key": KEY})
    assert started["expires_in"] == service.LINK_SECONDS
    link_id, poll = started["id"], started["poll"]

    # Nothing yet: the computer keeps waiting.
    info = service.link_info(FakeHandler(), {}, link_id)
    assert info["key"] == KEY

    box = {"v": 1, "e": "x", "n": "y", "c": "z"}
    result = {}

    def computer():
        result.update(
            service.poll_link(FakeHandler(f"/_dartchat/v1/link/{link_id}?poll={poll}"), {}, link_id)
        )

    waiting = threading.Thread(target=computer)
    waiting.start()
    time.sleep(0.2)
    answer = service.approve_link(FakeHandler(), {"box": box, "device_name": "Office PC"}, link_id)
    waiting.join(5)
    assert answer == {"device_id": "NEWDEVICE"}
    assert result == {
        "status": "done",
        "user_id": "@ana:chat.test",
        "device_id": "NEWDEVICE",
        "access_token": "computer-token",
        "box": box,
    }
    assert server["logins"][0]["type"] == "m.login.application_service"
    assert server["logins"][0]["initial_device_display_name"] == "Office PC"
    # The helper was registered once, for every account on this server only.
    register = [c for c in server["commands"] if c.startswith("appservices register")]
    assert len(register) == 1
    assert "regex: '@.*:chat\\.test'" in register[0]
    assert "exclusive: false" in register[0]
    # The code is used up.
    with pytest.raises(service.ApiError):
        service.link_info(FakeHandler(), {}, link_id)


def test_the_registration_is_kept_and_reused(server):
    for _ in range(2):
        started = service.start_link(FakeHandler(), {"key": KEY})
        service.approve_link(FakeHandler(), {"box": {"c": "z"}}, started["id"])
    register = [c for c in server["commands"] if c.startswith("appservices register")]
    assert len(register) == 1
    with open(service.LINK_FILE) as handle:
        assert json.load(handle)["as_token"]


def test_a_lost_registration_is_made_again(server):
    with open(service.LINK_FILE, "w") as handle:
        json.dump({"as_token": "old", "hs_token": "old"}, handle)
    started = service.start_link(FakeHandler(), {"key": KEY})
    service.approve_link(FakeHandler(), {"box": {"c": "z"}}, started["id"])
    assert len(server["logins"]) == 1


def test_the_wrong_poll_secret_sees_nothing(server):
    started = service.start_link(FakeHandler(), {"key": KEY})
    with pytest.raises(service.ApiError) as error:
        service.poll_link(FakeHandler("/x?poll=guess"), {}, started["id"])
    assert error.value.status == 404


def test_expired_codes_and_bad_keys_are_refused(server, monkeypatch):
    with pytest.raises(service.ApiError):
        service.start_link(FakeHandler(), {"key": "short"})
    started = service.start_link(FakeHandler(), {"key": KEY})
    service.links[started["id"]]["expires"] = time.time() - 1
    with pytest.raises(service.ApiError):
        service.approve_link(FakeHandler(), {"box": {"c": "z"}}, started["id"])
    assert server["logins"] == []


def test_too_many_waiting_codes(server):
    for _ in range(service.LINK_MAX_PENDING):
        service.start_link(FakeHandler(), {"key": KEY})
    with pytest.raises(service.ApiError) as error:
        service.start_link(FakeHandler(), {"key": KEY})
    assert error.value.status == 429
