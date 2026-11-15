"""Unit tests for the owner's ban and mute commands."""

from datetime import timezone

from group_bot.moderation import done_text, find_person, parse_command, parse_duration


def test_durations():
    assert parse_duration("30m") == 30
    assert parse_duration("2h") == 120
    assert parse_duration("1d") == 1440
    assert parse_duration("1w") == 10080
    assert parse_duration("1 D") == 1440
    assert parse_duration("0m") is None
    assert parse_duration("soon") is None
    assert parse_duration("9999w") is None


def test_commands():
    assert parse_command("hello there") is None
    assert parse_command("!ban @marko 1d") == ("ban", True, "marko", 1440)
    assert parse_command("!MUTE Marko Marković 2h") == ("mute", True, "Marko Marković", 120)
    assert parse_command("!unban @marko") == ("ban", False, "marko", None)
    assert parse_command("!unmute marko") == ("mute", False, "marko", None)
    assert parse_command("!ban @marko") == "help"
    assert parse_command("!ban @marko forever") == "help"
    assert parse_command("!unmute") == "help"


def test_finding_people():
    people = {"@marko:chat.test": "Marko M", "@ana:chat.test": "Ana", "@ana2:chat.test": "Ana"}
    assert find_person("marko", people) == "@marko:chat.test"
    assert find_person("@Marko:chat.test", people) == "@marko:chat.test"
    assert find_person("marko m", people) == "@marko:chat.test"
    assert find_person("ana2", people) == "@ana2:chat.test"
    assert find_person("Ana", people) is None  # two people are shown as Ana
    assert find_person("ghost", people) is None


def test_answers_say_when_it_ends():
    until = 1_794_758_400_000  # 2026-11-15 16:00 UTC
    assert done_text("mute", True, "Marko", until, timezone.utc).endswith("do 15.11. u 16:00.")
    assert "Marko" in done_text("ban", True, "Marko", until, timezone.utc)
    assert done_text("mute", False, "Marko", None, timezone.utc) == "🔊 Marko ponovo može da piše."
