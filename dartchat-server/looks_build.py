"""Builds the look catalog the apps download: themes, colour schemes,
backgrounds, message marks, reply looks, fonts, stickers and GIFs.

The packs that come with the add-on live in /looks. The owner can add their
own, in the same layout, under /share/dartchat/looks in Home Assistant; an
item there with the same id replaces the add-on's own.

The result is plain files that the front door (nginx) hands out as they are:

  <out>/catalog.json      the list of everything, fetched by the apps
  <out>/files/<hash>.<ext> pictures and fonts, named by their content, so a
                           phone downloads each one only once, ever

Nothing here is code: the apps only read colours, names, texts and pictures.
"""

import hashlib
import json
import os
import re
import shutil
import sys

FORMAT = 1
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
COLOUR = re.compile(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?")
FILE_TYPES = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}
MAX_FILE = 2 * 1024 * 1024
LISTS = ("themes", "schemes", "backgrounds", "statuses", "replies", "fonts")
# Switches the owner can turn off in Home Assistant. Everything is on unless
# switched off.
FEATURES = (
    "stickers",
    "gifs",
    "link_previews",
    "voice_to_text",
    "scheduled_send",
    "polls",
    "location",
)
# What the apps know how to draw; anything else is left out with a note.
DRAWN_STATUS = {
    "dots", "ticks", "circles", "darts", "bars",
    "hearts", "stars", "letters", "moon", "battery",
}
DRAWN_REPLIES = {"classic", "card", "bubble", "above", "quotes", "header", "dart"}
BACKGROUND_KINDS = {"plain", "dots", "darts", "grid", "rings", "gradient", "radial", "image"}
BUBBLE_STYLES = {"flat", "shadow", "glossy", "extreme"}


def log(message):
    print(f"[looks] {message}", flush=True)


class Builder:
    def __init__(self, out):
        self.out = out
        self.files = os.path.join(out, "files")
        self.problems = []

    def problem(self, where, what):
        self.problems.append(f"{where}: {what}")
        log(f"Left out {where}: {what}")

    def file(self, folder, name, where):
        """Copies a picture or font into the catalog and returns how the
        apps fetch it, or None when it cannot be used."""
        if not isinstance(name, str) or not name:
            self.problem(where, "no file named")
            return None
        path = os.path.normpath(os.path.join(folder, name))
        if not path.startswith(os.path.normpath(folder) + os.sep):
            self.problem(where, f"file {name} is outside the pack")
            return None
        ending = os.path.splitext(path)[1].lower()
        if ending not in FILE_TYPES:
            self.problem(where, f"{name} is not a PNG, WebP, GIF, JPEG or font")
            return None
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_FILE + 1)
        except OSError:
            self.problem(where, f"{name} is missing")
            return None
        if len(data) > MAX_FILE:
            self.problem(where, f"{name} is bigger than 2 MB")
            return None
        digest = hashlib.sha256(data).hexdigest()
        stored = digest[:32] + (".jpg" if ending == ".jpeg" else ending)
        with open(os.path.join(self.files, stored), "wb") as handle:
            handle.write(data)
        return {"path": "files/" + stored, "sha256": digest, "bytes": len(data)}

    def clean(self, item, kind, folder):
        where = f"{kind} {item.get('id', '?') if isinstance(item, dict) else '?'}"
        if not isinstance(item, dict):
            self.problem(where, "not an object")
            return None
        if not isinstance(item.get("id"), str) or not ID.fullmatch(item["id"]):
            self.problem(where, "id must be lowercase letters, digits and dashes")
            return None
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            self.problem(where, "no name")
            return None
        for key, value in item.items():
            if key in ("surface", "on_surface", "container", "on_container",
                       "chat_background", "bubble", "on_bubble", "other_bubble",
                       "on_other_bubble", "seed", "bubble_light", "bubble_dark",
                       "accent", "color", "read_color") and not (
                isinstance(value, str) and COLOUR.fullmatch(value)
            ):
                self.problem(where, f"{key} is not a colour like #12AB34")
                return None
        if "colors" in item and not (
            isinstance(item["colors"], list)
            and all(isinstance(c, str) and COLOUR.fullmatch(c) for c in item["colors"])
        ):
            self.problem(where, "colors must be a list of colours")
            return None
        if item.get("brightness") not in (None, "light", "dark"):
            self.problem(where, "brightness is light or dark")
            return None
        item = dict(item)
        if kind == "schemes":
            if item.get("style", "flat") not in BUBBLE_STYLES:
                self.problem(where, "style is flat, shadow, glossy or extreme")
                return None
            for key in ("seed", "bubble", "on_bubble", "accent"):
                if key not in item:
                    self.problem(where, f"{key} is missing")
                    return None
            radius = item.get("radius")
            if radius is not None and not (
                isinstance(radius, (int, float)) and 0 <= radius <= 40
            ):
                self.problem(where, "radius is a number from 0 to 40")
                return None
        elif kind == "backgrounds":
            if item.get("kind") not in BACKGROUND_KINDS:
                self.problem(where, "unknown kind")
                return None
            if item["kind"] == "image":
                picture = self.file(folder, item.get("image"), where)
                if not picture:
                    return None
                item["image"] = picture
                if item.get("fit", "tile") not in ("tile", "cover"):
                    self.problem(where, "fit is tile or cover")
                    return None
        elif kind == "statuses":
            texts = item.get("texts")
            if not (
                isinstance(texts, list) and len(texts) == 3
                and all(isinstance(t, str) and 0 < len(t) <= 12 for t in texts)
            ):
                self.problem(where, "texts must be three short texts: sent, delivered, read")
                return None
            if "draw" in item and item["draw"] not in DRAWN_STATUS:
                self.problem(where, f"the apps cannot draw {item['draw']}")
                return None
        elif kind == "replies":
            if item.get("draw") not in DRAWN_REPLIES:
                self.problem(where, f"the apps cannot draw {item.get('draw')}")
                return None
        elif kind == "fonts":
            font = self.file(folder, item.get("file"), where)
            if not font:
                return None
            item["file"] = font
        return item

    def merge(self, folders):
        lists = {kind: {} for kind in LISTS}
        packs = {"stickers": {}, "gifs": {}}
        for folder in folders:
            if not os.path.isdir(folder):
                continue
            for kind in LISTS:
                path = os.path.join(folder, kind + ".json")
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, encoding="utf-8") as handle:
                        items = json.load(handle)
                except (OSError, ValueError) as error:
                    self.problem(path, f"cannot be read ({error})")
                    continue
                if not isinstance(items, list):
                    self.problem(path, "must be a list")
                    continue
                for item in items:
                    cleaned = self.clean(item, kind, folder)
                    if cleaned:
                        # Same id again: the later folder (the owner's) wins,
                        # in the place the first one had.
                        lists[kind][cleaned["id"]] = cleaned
            for kind, member in (("stickers", "stickers"), ("gifs", "gifs")):
                base = os.path.join(folder, kind)
                if not os.path.isdir(base):
                    continue
                for name in sorted(os.listdir(base)):
                    pack = self.pack(os.path.join(base, name), kind, member)
                    if pack:
                        packs[kind][pack["id"]] = pack
        return lists, packs

    def pack(self, folder, kind, member):
        where = f"{kind} pack {os.path.basename(folder)}"
        try:
            with open(os.path.join(folder, "pack.json"), encoding="utf-8") as handle:
                pack = json.load(handle)
        except (OSError, ValueError) as error:
            self.problem(where, f"pack.json cannot be read ({error})")
            return None
        if not isinstance(pack, dict) or not ID.fullmatch(str(pack.get("id", ""))):
            self.problem(where, "pack.json needs an id")
            return None
        items = []
        for entry in pack.get(member) or []:
            if not isinstance(entry, dict) or not ID.fullmatch(str(entry.get("id", ""))):
                self.problem(where, "an item without a proper id")
                continue
            picture = self.file(folder, entry.get("file"), f"{where}/{entry['id']}")
            if picture:
                items.append({"id": entry["id"], "file": picture})
        if not items:
            self.problem(where, "nothing usable in it")
            return None
        return {"id": pack["id"], "name": str(pack.get("name") or pack["id"]), "items": items}


def parse_features(text):
    """'stickers=off, gifs=on' -> {'stickers': False, 'gifs': True, ...}"""
    features = {name: True for name in FEATURES}
    for part in re.split(r"[,\n]", text or ""):
        if "=" not in part:
            continue
        name, value = (s.strip().lower() for s in part.split("=", 1))
        if name in features:
            features[name] = value not in ("off", "false", "no", "0", "ne", "iskljuceno")
    return features


def build(folders, out, env=os.environ):
    builder = Builder(out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(builder.files)
    lists, packs = builder.merge(folders)
    catalog = {"format": FORMAT}
    for kind in LISTS:
        catalog[kind] = list(lists[kind].values())
    catalog["stickers"] = list(packs["stickers"].values())
    catalog["gifs"] = list(packs["gifs"].values())
    catalog["features"] = parse_features(env.get("DARTCHAT_FEATURES", ""))
    banner = (env.get("DARTCHAT_BANNER") or "").strip()
    if banner:
        catalog["banner"] = {
            "id": hashlib.sha256(banner.encode()).hexdigest()[:12],
            "text": banner[:300],
        }
    reactions = [r.strip() for r in (env.get("DARTCHAT_QUICK_REACTIONS") or "").split(",")]
    reactions = [r for r in reactions if r][:8]
    if reactions:
        catalog["quick_reactions"] = reactions
    share = (env.get("DARTCHAT_INVITE_SHARE_TEXT") or "").strip()
    if share:
        catalog["texts"] = {"invite_share": share[:500]}
    with open(os.path.join(out, "catalog.json"), "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, ensure_ascii=False, separators=(",", ":"))
    size = sum(
        os.path.getsize(os.path.join(builder.files, name))
        for name in os.listdir(builder.files)
    )
    log(
        f"Ready: {len(catalog['themes'])} themes, {len(catalog['schemes'])} colour"
        f" schemes, {len(catalog['backgrounds'])} backgrounds,"
        f" {len(catalog['statuses'])} message marks, {len(catalog['fonts'])} fonts,"
        f" {sum(len(p['items']) for p in catalog['stickers'])} stickers,"
        f" {sum(len(p['items']) for p in catalog['gifs'])} GIFs"
        f" ({size // 1024} KB of files)"
    )
    return catalog, builder.problems


def main(argv):
    if len(argv) < 3:
        print("usage: looks_build.py OUT FOLDER [FOLDER...]", file=sys.stderr)
        return 2
    build(argv[2:], argv[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
