"""Tests for problems the apps report into the add-on log."""

import pytest

import invite_service as service


class Handler:
    pass


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    monkeypatch.setattr(service, "caller", lambda handler: (handler.user, "t"))
    service.problem_counts.clear()


def as_user(user):
    handler = Handler()
    handler.user = user
    return handler


def test_logs_one_line_with_reason_and_app(capsys):
    answer = service.report_problem(
        as_user("@maretep:chat.test"),
        {
            "kind": "photo",
            "reason": "Could not reach the server.\nSocketException: failed",
            "platform": "android",
            "version": "1.189",
            "attempts": 4,
        },
    )
    assert answer == {"logged": True}
    line = capsys.readouterr().out.strip()
    assert line == (
        "[dartchat-app] @maretep:chat.test (android 1.189): photo not sent after 4 tries: "
        "Could not reach the server. SocketException: failed"
    )


def test_refuses_unknown_kind_and_empty_reason():
    with pytest.raises(service.ApiError):
        service.report_problem(as_user("@a:chat.test"), {"kind": "virus", "reason": "x"})
    with pytest.raises(service.ApiError):
        service.report_problem(as_user("@a:chat.test"), {"kind": "photo", "reason": "  "})


def test_long_reason_is_cut_and_flood_is_capped(capsys):
    user = as_user("@a:chat.test")
    service.report_problem(user, {"kind": "voice", "reason": "x" * 1000})
    assert len(capsys.readouterr().out) < 400
    for _ in range(service.PROBLEMS_PER_HOUR + 5):
        last = service.report_problem(user, {"kind": "voice", "reason": "again"})
    assert last == {"logged": False}
