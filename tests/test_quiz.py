"""Tests for Dart Chat quiz tokens: the app gets one, the quiz server checks it."""

import pytest

import invite_service as service

KEY = "quiz-test-key-0123456789"


class Handler:
    def __init__(self, user=None, key=None):
        self.user = user
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}


@pytest.fixture
def quiz(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "SERVER_NAME", "chat.test")
    monkeypatch.setattr(service, "BOT_USER", "@dartbot:chat.test")
    monkeypatch.setattr(service, "QUIZ_KEY", KEY)
    monkeypatch.setattr(service, "store", service.Store(str(tmp_path / "s.json")))
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, "t"))
    monkeypatch.setattr(service, "current_display_name", lambda user, token: "Marko")


def token_for(user):
    return service.quiz_token(Handler(user=user), {})


def whoami(token, key=KEY):
    return service.quiz_whoami(Handler(key=key), {"openid_token": token})


def test_token_names_the_account(quiz):
    answer = token_for("@marko:chat.test")
    assert answer["expires_in"] == 3600
    assert len(answer["token"]) <= 512
    who = whoami(answer["token"])
    assert who["matrix_id"] == "@marko:chat.test"
    assert who["displayname"] == "Marko"
    assert 3500 < who["expires_in"] <= 3600


def test_wrong_quiz_key_is_refused(quiz):
    token = token_for("@marko:chat.test")["token"]
    with pytest.raises(service.ApiError) as error:
        whoami(token, key="wrong")
    assert error.value.status == 401
    with pytest.raises(service.ApiError):
        whoami(token, key=None)


@pytest.mark.parametrize("bad", ["", "x", "dcq1.a.b", None, "dcq1." + "a" * 600])
def test_garbage_tokens_are_refused(quiz, bad):
    with pytest.raises(service.ApiError) as error:
        whoami(bad)
    assert error.value.status == 401


def test_changed_token_is_refused(quiz):
    token = token_for("@marko:chat.test")["token"]
    head, payload, signature = token.split(".")
    other = service.b64url(b'{"u":"@dottore:chat.test","n":"","e":9999999999}')
    with pytest.raises(service.ApiError):
        whoami(f"{head}.{other}.{signature}")


def test_expired_token_is_refused(quiz, monkeypatch):
    token = token_for("@marko:chat.test")["token"]
    now = service.time.time()
    monkeypatch.setattr(service.time, "time", lambda: now + 3601)
    with pytest.raises(service.ApiError):
        whoami(token)


def test_closed_account_loses_its_token(quiz):
    token = token_for("@marko:chat.test")["token"]
    service.store.data["deactivated"].append("@marko:chat.test")
    with pytest.raises(service.ApiError):
        whoami(token)
    with pytest.raises(service.ApiError) as error:
        token_for("@marko:chat.test")
    assert error.value.status == 403


def test_bot_gets_no_token(quiz):
    with pytest.raises(service.ApiError) as error:
        token_for("@dartbot:chat.test")
    assert error.value.status == 403


def test_no_quiz_key_means_no_quiz(quiz, monkeypatch):
    monkeypatch.setattr(service, "QUIZ_KEY", "")
    with pytest.raises(service.ApiError) as error:
        token_for("@marko:chat.test")
    assert error.value.status == 404
    with pytest.raises(service.ApiError) as error:
        whoami("dcq1.a.b", key="")
    assert error.value.status == 404
