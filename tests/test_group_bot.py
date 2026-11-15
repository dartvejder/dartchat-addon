"""Unit tests for the group rules, without a chat server."""

import asyncio
from datetime import datetime

from group_bot.core import Bot, Message, without_quote
from group_bot.rules import RULES, RoomRules, Settings, describe
from group_bot.store import Store

T0 = 1_793_500_000_000
ROOM = "!grupa"


class Sent(list):
    async def __call__(self, room_id, text, reply_to):
        self.append((room_id, text, reply_to))
        return True


def make_bot(tmp_path, rules=None, options=None, titles=None):
    room_rules = RoomRules(str(tmp_path / "rooms.json"))
    room_rules.set(ROOM, {r: True for r in RULES} if rules is None else rules)
    sent = Sent()
    settings = Settings({"expand_short_links": False, **(options or {})})
    bot = Bot(settings, room_rules, Store(":memory:"), sent)
    titles = titles or {}

    async def fetch_title(url):
        return titles.get(url)

    bot.fetch_title = fetch_title
    bot.report_delays = (0,)
    return bot, sent


def msg(body, n, who="@ana:t", name="Ana", room=ROOM, **kw):
    return Message(room, f"$e{n}", who, name, body, T0 + n * 60_000, **kw)


def run(bot, *messages):
    for m in messages:
        asyncio.run(bot.handle(m))


def test_repost_reply_quotes_first_post(tmp_path):
    bot, sent = make_bot(tmp_path)
    run(bot, msg("vidi https://youtu.be/dQw4w9WgXcQ?si=x", 1),
        msg("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 2, "@marko:t", "Marko"))
    assert sent == [(ROOM, "Idi u cosak!\nMarko je repostovao 1 put", "$e1")]
    run(bot, msg("https://youtube.com/shorts/dQw4w9WgXcQ", 3, "@marko:t", "Marko"))
    assert sent[-1][1].endswith("Marko je repostovao 2 puta")


def test_own_repost(tmp_path):
    bot, sent = make_bot(tmp_path)
    run(bot, msg("https://x.com/a/status/1", 1), msg("https://twitter.com/b/status/1", 2))
    assert sent[0][1].startswith("Repostovao si svoj link, idi u cosak duplo!")


def test_rules_are_per_group(tmp_path):
    bot, sent = make_bot(tmp_path)
    other = "!druga"
    run(bot, msg("https://x.com/a/status/1", 1, room=other),
        msg("https://x.com/a/status/1", 2, "@marko:t", room=other))
    assert sent == []
    bot.rules.set(ROOM, {"commands": True})
    run(bot, msg("https://x.com/a/status/1", 3), msg("https://x.com/a/status/1", 4, "@m:t"))
    assert sent == []


def test_quoted_reply_is_not_a_repost(tmp_path):
    bot, sent = make_bot(tmp_path)
    run(bot, msg("https://x.com/a/status/1", 1),
        msg("> <@ana:t> https://x.com/a/status/1\n\nhaha", 2, "@m:t"))
    assert sent == []
    assert without_quote("> a\n> b\n\nok") == "ok"


def test_deleted_first_post_is_forgotten(tmp_path):
    bot, sent = make_bot(tmp_path)
    run(bot, msg("https://x.com/a/status/1", 1))
    bot.redacted(ROOM, "$e1")
    run(bot, msg("https://x.com/a/status/1", 2, "@m:t"))
    assert sent == []


def test_commands(tmp_path):
    bot, sent = make_bot(tmp_path)
    run(bot, msg("https://x.com/a/status/1", 1), msg("https://x.com/a/status/1", 2, "@m:t", "Marko"))
    run(bot, msg("  !Lista ", 3))
    assert sent[-1] == (ROOM, "Lista ponavljača:\n1. Marko je repostovao 1 put", None)
    run(bot, msg("!stats", 4))
    assert "Poruka:" in sent[-1][1]
    run(bot, msg("!top", 5))
    assert "Top lista" in sent[-1][1]
    bot.rules.set(ROOM, {"repost": True})
    count = len(sent)
    run(bot, msg("!lista", 6))
    assert len(sent) == count


def test_same_news_from_another_site(tmp_path):
    titles = {"https://n1info.rs/vesti/izbori/": "Vlada najavila izbore za decembar - N1",
              "https://www.blic.rs/vesti/abc": "Izbori u decembru, najavila Vlada | Blic"}
    bot, sent = make_bot(tmp_path, titles=titles)
    run(bot, msg("https://n1info.rs/vesti/izbori/", 1),
        msg("https://www.blic.rs/vesti/abc", 2, "@m:t", "Marko"))
    assert sent == [(ROOM, "Ovo je već bilo, samo sa drugog sajta 👀\nMarko je repostovao 1 put",
                     "$e1")]


def test_keyword_rule(tmp_path):
    options = {"keyword_words": "Đoković", "keyword_rule_name": "Novak",
               "keyword_reply_text": "Opet Novak!", "keyword_commands": "!novak"}
    bot, sent = make_bot(tmp_path, options=options)
    assert "keyword" in [r["id"] for r in describe(bot.cfg)]
    run(bot, msg("https://sport.rs/tenis/novak-djokovic-pobeda", 1))
    assert sent == [(ROOM, "Opet Novak!\nAna: 1", "$e1")]
    run(bot, msg("!novak", 2))
    assert sent[-1] == (ROOM, "Novak:\n1. Ana: 1", None)


def test_keyword_rule_hidden_without_words(tmp_path):
    bot, sent = make_bot(tmp_path)
    assert "keyword" not in [r["id"] for r in describe(bot.cfg)]
    run(bot, msg("https://sport.rs/tenis/novak-djokovic", 1))
    assert sent == []


def test_midnight_report(tmp_path):
    bot, sent = make_bot(tmp_path)
    day = datetime.fromtimestamp(T0 / 1000, bot.cfg.tz).date()
    run(bot, msg("zdravo", 1), msg("https://x.com/a/status/1", 2, "@m:t", "Marko"))
    bot.today = lambda: day.fromordinal(day.toordinal() + 1)
    bot.stats.set_state("last_report_day", day.fromordinal(day.toordinal() - 1).isoformat())
    asyncio.run(bot.tick())
    texts = [t for _, t, _ in sent]
    assert texts[0].startswith("📊 Statistika za") and "Poruka: 2" in texts[0]
    assert texts[1].startswith("🏆 Top lista za")
    sent.clear()
    asyncio.run(bot.tick())
    assert sent == []


def test_rules_file_round_trip(tmp_path):
    rules = RoomRules(str(tmp_path / "rooms.json"))
    rules.set("!a", {"repost": True, "nonsense": True})
    assert RoomRules(str(tmp_path / "rooms.json")).get("!a")["repost"] is True
    rules.set("!a", {})
    assert RoomRules(str(tmp_path / "rooms.json")).active_rooms() == []
