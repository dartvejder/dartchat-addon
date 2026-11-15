"""Find links in a message and reduce each one to a canonical key.

Two links that point at the same post should produce the same key, even when
one was shared from the mobile app with tracking parameters and the other was
copied from a desktop browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(
    r"""(?:https?://|www\.)[^\s<>"'`]+""",
    re.IGNORECASE,
)
TRAILING_PUNCT = ".,;:!?)]}>'\"…"

# Hosts that are just mirrors / alternate front-ends of the same site.
HOST_ALIASES = {
    "twitter.com": "x.com",
    "mobile.twitter.com": "x.com",
    "mobile.x.com": "x.com",
    "fxtwitter.com": "x.com",
    "vxtwitter.com": "x.com",
    "fixupx.com": "x.com",
    "fixvx.com": "x.com",
    "twittpr.com": "x.com",
    "nitter.net": "x.com",
    "xcancel.com": "x.com",
    "ddinstagram.com": "instagram.com",
    "instagramez.com": "instagram.com",
    "kkinstagram.com": "instagram.com",
    "instagr.am": "instagram.com",
    "vxtiktok.com": "tiktok.com",
    "tnktok.com": "tiktok.com",
    "m.youtube.com": "youtube.com",
    "music.youtube.com": "youtube.com",
    "youtube-nocookie.com": "youtube.com",
    "old.reddit.com": "reddit.com",
    "new.reddit.com": "reddit.com",
    "np.reddit.com": "reddit.com",
    "rxddit.com": "reddit.com",
    "vxreddit.com": "reddit.com",
    "m.facebook.com": "facebook.com",
    "mbasic.facebook.com": "facebook.com",
    "web.facebook.com": "facebook.com",
    "fb.com": "facebook.com",
}

# Short links whose target can only be found by following the redirect.
SHORTENER_HOSTS = {
    "t.co", "bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "buff.ly", "is.gd",
    "vm.tiktok.com", "vt.tiktok.com", "fb.watch", "lnkd.in", "pin.it",
    "on.soundcloud.com", "spoti.fi", "amzn.to", "a.co", "dlvr.it",
}

# Query parameters that never change what a page shows.
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igsh", "igshid",
    "si", "ref", "ref_src", "ref_url", "feature", "share_id",
    "is_from_webapp", "sender_device", "web_id", "_r", "_t", "mibextid",
    "rdid", "share_app_id", "share_link_id", "context", "utm_id", "pp",
    "embeds_referring_euri", "embeds_referring_origin", "source_ve_path",
}


@dataclass(frozen=True)
class Link:
    original: str  # as the user typed it
    key: str       # canonical identity used for duplicate detection


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    out = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(TRAILING_PUNCT)
        # Keep a closing paren if the URL itself opened one (Wikipedia style).
        if url.count("(") > url.count(")") and m.group(0)[len(url):].startswith(")"):
            url += ")"
        if url.lower().startswith("www."):
            url = "https://" + url
        out.append(url)
    return out


def _clean_host(host: str) -> str:
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if host in HOST_ALIASES:
        return HOST_ALIASES[host]
    if host.startswith("m.") and host[2:] in HOST_ALIASES.values():
        return host[2:]
    return host


def is_shortener(url: str) -> bool:
    parts = urlsplit(url)
    host = _clean_host(parts.hostname or "")
    path = parts.path
    if host in SHORTENER_HOSTS:
        return True
    # Share links that hide the real post id.
    if host == "reddit.com" and re.match(r"^/r/[^/]+/s/", path):
        return True
    if host == "instagram.com" and path.startswith("/share/"):
        return True
    if host == "facebook.com" and path.startswith("/share/"):
        return True
    if host == "tiktok.com" and path.startswith("/t/"):
        return True
    return False


def canonical_key(url: str) -> str:
    """Return a stable identity string for a URL."""
    parts = urlsplit(url.strip())
    host = _clean_host(parts.hostname or "")
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    segs = [s for s in path.split("/") if s]
    query = parse_qsl(parts.query, keep_blank_values=False)
    q = dict(query)

    # --- site specific rules: reduce to the post id ---------------------------
    if host == "x.com":
        # /user/status/123, /i/web/status/123, /user/status/123/photo/1
        if "status" in segs:
            i = segs.index("status")
            if i + 1 < len(segs) and segs[i + 1].isdigit():
                return f"x:{segs[i + 1]}"
    if host == "instagram.com":
        # /p/ID, /reel/ID, /reels/ID, /tv/ID, /username/p/ID, /username/reel/ID
        for i, s in enumerate(segs):
            if s in ("p", "reel", "reels", "tv") and i + 1 < len(segs):
                return f"instagram:{segs[i + 1]}"
        if segs[:1] == ["stories"] and len(segs) >= 3:
            return f"instagram:story:{segs[2]}"
    if host == "tiktok.com":
        for i, s in enumerate(segs):
            if s in ("video", "photo") and i + 1 < len(segs):
                return f"tiktok:{segs[i + 1]}"
    if host == "youtu.be" and segs:
        return f"youtube:{segs[0]}"
    if host == "youtube.com":
        if "v" in q:
            return f"youtube:{q['v']}"
        if len(segs) >= 2 and segs[0] in ("shorts", "embed", "live", "v", "e"):
            return f"youtube:{segs[1]}"
    if host == "reddit.com" and "comments" in segs:
        i = segs.index("comments")
        if i + 1 < len(segs):
            post = segs[i + 1].lower()
            # A link to a specific comment is a different thing from the post.
            if i + 3 < len(segs):
                return f"reddit:{post}:{segs[i + 3].lower()}"
            return f"reddit:{post}"
    if host == "redd.it" and segs:
        return f"reddit:{segs[0].lower()}"
    if host == "bsky.app" and len(segs) >= 4 and segs[0] == "profile" and segs[2] == "post":
        return f"bsky:{segs[1].lower()}:{segs[3]}"
    if host == "facebook.com":
        for name in ("story_fbid", "fbid", "v"):
            if name in q:
                return f"facebook:{q[name]}"
        for i, s in enumerate(segs):
            if s in ("posts", "videos", "reel", "permalink") and i + 1 < len(segs):
                return f"facebook:{segs[i + 1]}"
    if host == "open.spotify.com":
        segs = [s for s in segs if not s.startswith("intl-")]
        if len(segs) >= 2:
            return f"spotify:{segs[0]}:{segs[1]}"

    # --- generic rule ---------------------------------------------------------
    kept = sorted(
        (k, v) for k, v in query
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    )
    path = "/" + "/".join(segs)
    return urlunsplit(("", host, path.rstrip("/") or "/", urlencode(kept), "")).lstrip("/")
