"""The look catalog: the add-on's own packs, the owner's packs and the
switches from the add-on options."""

import hashlib
import json
import os

import looks_build

PACKS = os.path.join(os.path.dirname(__file__), "..", "dartchat-server", "looks")


def build(tmp_path, *folders, env=None):
    out = tmp_path / "out"
    catalog, problems = looks_build.build([str(f) for f in folders], str(out), env or {})
    on_disk = json.loads((out / "catalog.json").read_text(encoding="utf-8"))
    assert on_disk == catalog
    return catalog, problems, out


def test_the_add_on_packs_are_all_usable(tmp_path):
    catalog, problems, out = build(tmp_path, PACKS)
    assert problems == []
    ids = lambda kind: [item["id"] for item in catalog[kind]]
    # Everything the app had built in is on the server too, in the same order.
    assert ids("themes")[:11] == [
        "standard", "terminal", "amoled", "paper", "midnight", "vampire",
        "frost", "forest", "typewriter", "grey", "candy",
    ]
    assert ids("schemes")[0] == "lime" and ids("schemes")[-1] == "terminal"
    assert ids("backgrounds")[0] == "grid" and ids("backgrounds")[-1] == "plain"
    assert len(catalog["statuses"]) >= 10 and len(catalog["replies"]) == 7
    # Every file is where the catalog says, with the size and hash it says.
    files = [f["file"] for f in catalog["fonts"]]
    files += [b["image"] for b in catalog["backgrounds"] if b["kind"] == "image"]
    files += [i["file"] for p in catalog["stickers"] + catalog["gifs"] for i in p["items"]]
    assert len(files) >= 15
    for entry in files:
        data = (out / entry["path"]).read_bytes()
        assert len(data) == entry["bytes"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    # Small enough to stay free: the list itself is a few KB.
    assert (out / "catalog.json").stat().st_size < 40 * 1024


def test_themes_point_at_fonts_and_backgrounds_that_exist(tmp_path):
    catalog, _, _ = build(tmp_path, PACKS)
    fonts = {f["id"] for f in catalog["fonts"]} | {"JetBrainsMono"}
    backgrounds = {b["id"] for b in catalog["backgrounds"]}
    statuses = {s["id"] for s in catalog["statuses"]}
    for theme in catalog["themes"]:
        assert theme.get("font", "JetBrainsMono") in fonts, theme
        assert theme.get("background", "grid") in backgrounds, theme
        assert theme.get("status", "dots") in statuses, theme


def test_the_owners_packs_replace_and_add(tmp_path):
    own = tmp_path / "own"
    (own / "stickers" / "mine").mkdir(parents=True)
    (own / "themes.json").write_text(json.dumps([
        {"id": "paper", "name": "My paper", "surface": "#FFFFFF"},
        {"id": "new-one", "name": "New one", "surface": "#000000"},
        {"id": "Bad Id", "name": "x"},
        {"id": "bad-colour", "name": "x", "surface": "red"},
    ]))
    (own / "stickers" / "mine" / "pack.json").write_text(json.dumps(
        {"id": "mine", "name": "Mine", "stickers": [
            {"id": "a", "file": "a.png"},
            {"id": "escape", "file": "../../etc/passwd"},
            {"id": "script", "file": "a.js"},
        ]}))
    (own / "stickers" / "mine" / "a.png").write_bytes(b"\x89PNG fake")
    catalog, problems, _ = build(tmp_path, PACKS, own)
    themes = {t["id"]: t for t in catalog["themes"]}
    assert themes["paper"]["name"] == "My paper"
    assert [t["id"] for t in catalog["themes"]].index("paper") == 3
    assert "new-one" in themes and "bad-colour" not in themes
    mine = [p for p in catalog["stickers"] if p["id"] == "mine"][0]
    assert [i["id"] for i in mine["items"]] == ["a"]
    assert len(problems) == 4


def test_switches_notice_reactions_and_share_text(tmp_path):
    catalog, _, _ = build(tmp_path, PACKS, env={
        "DARTCHAT_FEATURES": "stickers=off, gifs = on, nonsense=off",
        "DARTCHAT_BANNER": "Server se restartuje u 22h",
        "DARTCHAT_QUICK_REACTIONS": "🎯, 😂,❤️,,",
        "DARTCHAT_INVITE_SHARE_TEXT": "Dođi na Dart Chat: {link}",
    })
    assert catalog["features"]["stickers"] is False
    assert catalog["features"]["gifs"] is True
    assert "nonsense" not in catalog["features"]
    assert catalog["banner"]["text"] == "Server se restartuje u 22h"
    assert len(catalog["banner"]["id"]) == 12
    assert catalog["quick_reactions"] == ["🎯", "😂", "❤️"]
    assert catalog["texts"]["invite_share"] == "Dođi na Dart Chat: {link}"


def test_nothing_set_means_everything_on_and_no_notice(tmp_path):
    catalog, _, _ = build(tmp_path, PACKS)
    assert all(catalog["features"].values())
    assert "banner" not in catalog and "quick_reactions" not in catalog
    assert "texts" not in catalog


def test_a_missing_owner_folder_is_fine(tmp_path):
    catalog, problems, _ = build(tmp_path, PACKS, tmp_path / "nowhere")
    assert problems == [] and catalog["themes"]
