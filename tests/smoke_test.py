#!/usr/bin/env python3
"""Smoke test for a running Dart Chat Server add-on container.

Usage: smoke_test.py <server url> <one-time first-account token>

Expects the container to be started with
DARTCHAT_RESEND_BASE=http://127.0.0.1:6190 and a resend_api_key, so this
script can stand in for the email service.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE, FIRST_TOKEN = sys.argv[1], sys.argv[2]
MATRIX = "/_matrix/client/v3"
INVITE = "/_dartchat/v1"
mails = []


class FakeResend(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body["auth"] = self.headers["Authorization"]
        mails.append(body)
        refuse = body["to"][0].startswith("fail")
        data = json.dumps({"message": "Domain not verified"} if refuse else {"id": "1"})
        self.send_response(403 if refuse else 200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data.encode())

    def log_message(self, *args):
        pass


def call(method, path, body=None, token=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {}


def expect(result, status, **fields):
    got_status, body = result
    assert got_status == status, f"expected {status}, got {got_status} {body}"
    for key, value in fields.items():
        assert body.get(key) == value, f"expected {key}={value!r}, got {body}"
    return body


def login(username, password):
    return call(
        "POST",
        MATRIX + "/login",
        {
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": username},
            "password": password,
        },
    )


def circles_test(owner, join):
    """Two circles cannot find, see or contact each other."""

    def invited(email, circles):
        body = {"email": email, "circles": circles}
        return expect(call("POST", INVITE + "/admin/invites", body, owner), 200)["code"]

    def account(name, circles):
        expect(join(invited(f"{name}@example.com", circles), name), 200)
        return expect(login(name, "ana-password-1"), 200)["access_token"]

    def profile(token, name):
        return call("GET", f"{MATRIX}/profile/@{name}:chat.test", token=token)

    def search(token, term):
        body = expect(
            call("POST", MATRIX + "/user_directory/search", {"search_term": term}, token),
            200,
        )
        return {r["user_id"] for r in body["results"]}

    def create_room(token, invite):
        body = {
            "invite": [f"@{name}:chat.test" for name in invite],
            "preset": "private_chat",
            "is_direct": len(invite) == 1,
        }
        return call("POST", MATRIX + "/createRoom", body, token)

    # Before any circle exists everyone can see everyone.
    early = account("early", [])
    expect(profile(early, "boss"), 200)

    school = expect(
        call("POST", INVITE + "/admin/circles", {"name": "School"}, owner), 200
    )["id"]
    work = expect(call("POST", INVITE + "/admin/circles", {"name": "Work"}, owner), 200)["id"]
    expect(call("POST", INVITE + "/admin/circles", {"name": ""}, owner), 400)
    expect(call("GET", INVITE + "/admin/circles", token=early), 403)
    expect(
        call("POST", INVITE + "/admin/invites", {"email": "x@example.com", "circles": ["nope"]}, owner),
        400,
    )

    sam = account("sam", [school])
    sue = account("sue", [school])
    wes = account("wes", [work])
    listed = expect(call("GET", INVITE + "/admin/circles", token=owner), 200)["circles"]
    members = {c["name"]: c["members"] for c in listed}
    assert members == {
        "School": ["@sam:chat.test", "@sue:chat.test"],
        "Work": ["@wes:chat.test"],
    }, members

    # Same circle: visible. Other circle: looks like it does not exist.
    expect(profile(sam, "sue"), 200)
    expect(profile(sam, "wes"), 404, errcode="M_NOT_FOUND")
    expect(profile(wes, "sam"), 404, errcode="M_NOT_FOUND")
    expect(call("GET", f"{MATRIX}/profile/@wes:chat.test/displayname", token=sam), 404)
    expect(call("GET", f"{MATRIX}/profile/%40wes%3Achat.test", token=sam), 404)
    expect(call("GET", f"{MATRIX}/profile/@wes:chat.test"), 401)
    expect(call("GET", f"{MATRIX}/presence/@wes:chat.test/status", token=sam), 404)
    # Online status switched off: friends see offline and no "last seen".
    presence_path = f"{MATRIX}/presence/@sue:chat.test/status"
    expect(
        call("PUT", presence_path, {"presence": "online", "dartchat_hidden": True}, sue),
        200,
    )
    hidden = expect(call("GET", presence_path, token=sam), 200)
    assert hidden == {"presence": "offline"}, hidden
    # Her own status is not hidden from herself: the chat server answers.
    own = call("GET", presence_path, token=sue)
    assert own != (200, {"presence": "offline"}), own
    expect(
        call("PUT", presence_path, {"presence": "online", "dartchat_hidden": False}, sue),
        200,
    )
    # Shown again: the chat server answers, whatever it knows.
    shown = call("GET", presence_path, token=sam)
    assert shown != (200, {"presence": "offline"}), shown
    # Everyone sees the owner, the owner sees everyone, no circle sees only the owner.
    expect(profile(sam, "boss"), 200)
    expect(profile(owner, "wes"), 200)
    expect(profile(early, "sam"), 404)
    expect(profile(early, "boss"), 200)
    assert "@wes:chat.test" not in search(sam, "wes"), "search leaks the other circle"
    assert "@wes:chat.test" not in search(sam, "chat.test")

    # Contact: chats and invitations only inside a circle.
    expect(create_room(sam, ["wes"]), 404, errcode="M_NOT_FOUND")
    expect(create_room(sam, ["sue", "wes"]), 404)
    room = expect(create_room(sam, ["sue"]), 200)["room_id"]
    invite_path = f"{MATRIX}/rooms/{room}/invite"
    expect(call("POST", invite_path, {"user_id": "@wes:chat.test"}, sam), 404)
    expect(
        call(
            "PUT",
            f"{MATRIX}/rooms/{room}/state/m.room.member/@wes:chat.test",
            {"membership": "invite"},
            sam,
        ),
        404,
    )
    # Groups: friends make them with people from their own circle; the
    # owner may be in any group.
    expect(create_room(early, ["boss"]), 200)  # private chat with the owner
    expect(create_room(early, ["boss", "boss"]), 403, errcode="DARTCHAT_NOT_ONE_CIRCLE")
    expect(create_room(sam, []), 200)
    party = expect(
        call("POST", MATRIX + "/createRoom", {"name": "Party", "invite": ["@sue:chat.test"]}, sam),
        200,
    )["room_id"]
    expect(
        call(
            "POST",
            MATRIX + "/createRoom",
            {"name": "Open", "invite": ["@sue:chat.test"], "preset": "public_chat"},
            sam,
        ),
        403,
        errcode="DARTCHAT_NOT_ONE_CIRCLE",
    )
    expect(call("POST", MATRIX + "/createRoom", "nonsense", sam), 400)
    expect(call("POST", f"{MATRIX}/rooms/{party}/invite", {"user_id": "@boss:chat.test"}, sam), 200)
    # Who sam can pick from: the circle and the owner.
    people = expect(call("GET", INVITE + "/people", token=sam), 200)
    assert people["people"] == ["@boss:chat.test", "@sue:chat.test"], people
    assert people["groups"] is True and people["owner"] is False, people
    assert [c["name"] for c in people["circles"]] == ["School"], people
    lonely = expect(call("GET", INVITE + "/people", token=early), 200)
    assert lonely["people"] == ["@boss:chat.test"] and lonely["groups"] is False, lonely
    everyone = expect(call("GET", INVITE + "/people", token=owner), 200)
    assert everyone["owner"] is True and everyone["groups"] is True, everyone
    assert {"@sam:chat.test", "@wes:chat.test", "@early:chat.test"} <= set(
        everyone["people"]
    ), everyone
    expect(call("GET", INVITE + "/people"), 401)
    expect(call("POST", invite_path, {}, sam), 400)
    club = expect(create_room(owner, ["sam", "sue"]), 200)["room_id"]
    expect(call("POST", f"{MATRIX}/rooms/{club}/join", {}, sam), 200)
    expect(
        call("POST", f"{MATRIX}/rooms/{club}/invite", {"user_id": "@early:chat.test"}, sam),
        404,
    )
    expect(
        call("POST", f"{MATRIX}/rooms/{club}/invite", {"user_id": "@early:chat.test"}, owner),
        200,
    )
    keys = expect(
        call(
            "POST",
            MATRIX + "/keys/query",
            {"device_keys": {"@wes:chat.test": [], "@sue:chat.test": []}},
            sam,
        ),
        200,
    )
    assert "@wes:chat.test" not in keys.get("device_keys", {}), keys

    # People the owner puts in one chat do see each other there.
    mixed = expect(create_room(owner, ["sam", "wes"]), 200)["room_id"]
    expect(call("POST", f"{MATRIX}/rooms/{mixed}/join", {}, sam), 200)
    expect(call("POST", f"{MATRIX}/rooms/{mixed}/join", {}, wes), 200)
    time.sleep(31)  # the shared-chat lookup is cached for half a minute
    expect(profile(sam, "wes"), 200)
    expect(create_room(sam, ["wes"]), 404)

    # Moving someone into a circle takes effect at once.
    expect(
        call(
            "PUT",
            f"{INVITE}/admin/circles/{work}",
            {"members": ["@wes:chat.test", "@sue:chat.test"]},
            owner,
        ),
        200,
    )
    expect(create_room(sue, ["wes"]), 200)
    # sue is in both circles, but a group stays inside one of them.
    expect(create_room(sue, ["sam", "wes"]), 403, errcode="DARTCHAT_NOT_ONE_CIRCLE")
    duo = expect(create_room(sue, ["sam"]), 200)["room_id"]
    duo_invite = f"{MATRIX}/rooms/{duo}/invite"
    expect(call("POST", duo_invite, {"user_id": "@wes:chat.test"}, sue), 403)
    expect(
        call(
            "PUT",
            f"{MATRIX}/rooms/{duo}/state/m.room.member/@wes:chat.test",
            {"membership": "invite"},
            sue,
        ),
        403,
        errcode="DARTCHAT_NOT_ONE_CIRCLE",
    )
    expect(call("DELETE", f"{INVITE}/admin/circles/{work}", token=owner), 200)
    expect(profile(sue, "wes"), 200)  # they share a chat now
    expect(create_room(sue, ["wes"]), 404)

    # Taken out of the circle: the private chat between them is closed for
    # both. The owner's group with both of them stays.
    expect(call("POST", f"{MATRIX}/rooms/{room}/join", {}, sue), 200)
    expect(
        call(
            "PUT",
            f"{INVITE}/admin/circles/{school}",
            {"members": ["@sam:chat.test"]},
            owner,
        ),
        200,
    )

    def joined(token):
        return expect(call("GET", MATRIX + "/joined_rooms", token=token), 200)[
            "joined_rooms"
        ]

    for _ in range(60):
        if room not in joined(sam) and room not in joined(sue):
            break
        time.sleep(1)
    assert room not in joined(sam), "the private chat stayed open for sam"
    assert room not in joined(sue), "the private chat stayed open for sue"
    assert club in joined(sam), "the owner's group was closed too"

    # Strangers cannot probe usernames, and direct sign-up is closed.
    expect(login("sam", "wrong-password"), 403, errcode="M_FORBIDDEN")
    expect(login("nobody-here", "wrong-password"), 403, errcode="M_FORBIDDEN")
    expect(
        call("POST", MATRIX + "/register", {"username": "sam", "password": "x" * 12}),
        403,
        errcode="M_FORBIDDEN",
    )
    expect(call("GET", MATRIX + "/register/available?username=sam"), 403)
    expect(call("GET", "/_matrix/federation/v1/version"), 404)


def scheduled_test(owner):
    """A message handed over encrypted is sent later in the sender's name."""
    path = INVITE + "/scheduled"
    room = expect(
        call("POST", MATRIX + "/createRoom", {"name": "Later"}, owner), 200
    )["room_id"]
    now = int(time.time() * 1000)
    sealed = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "ciphertext": "not-really-encrypted-but-shaped-like-it",
        "session_id": "session",
    }
    expect(call("GET", path), 401)
    expect(call("GET", path, token=owner), 200, scheduled=[])
    # Text that is not encrypted is refused: the server must never hold it.
    plain = {"room_id": room, "send_at": now, "content": {"msgtype": "m.text", "body": "hi"}}
    expect(call("POST", path, plain, owner), 400, errcode="DARTCHAT_NOT_ENCRYPTED")
    expect(
        call("POST", path, {"room_id": "!nope:chat.test", "send_at": now, "content": sealed}, owner),
        403,
    )
    expect(call("POST", path, {"room_id": room, "send_at": "soon", "content": sealed}, owner), 400)
    soon = expect(
        call("POST", path, {"room_id": room, "send_at": now + 4000, "content": sealed}, owner),
        200,
    )
    assert soon["status"] == "waiting", soon
    never = expect(
        call("POST", path, {"room_id": room, "send_at": now + 3600000, "content": sealed}, owner),
        200,
    )
    listed = expect(call("GET", path, token=owner), 200)["scheduled"]
    assert [e["id"] for e in listed] == [soon["id"], never["id"]], listed
    assert "token" not in listed[0] and "content" not in listed[0], listed
    expect(call("DELETE", f"{path}/{never['id']}", token=owner), 200)
    expect(call("DELETE", f"{path}/{never['id']}", token=owner), 404)

    def sent():
        chunk = expect(
            call("GET", f"{MATRIX}/rooms/{room}/messages?dir=b&limit=10", token=owner), 200
        )["chunk"]
        return [e for e in chunk if e["type"] == "m.room.encrypted"]

    assert sent() == [], "sent before its time"
    for _ in range(30):
        if sent():
            break
        time.sleep(1)
    events = sent()
    assert len(events) == 1 and events[0]["sender"] == "@boss:chat.test", events
    assert events[0]["content"]["ciphertext"] == sealed["ciphertext"], events
    expect(call("GET", path, token=owner), 200, scheduled=[])


def renamed_test(owner, friend, friend_id):
    """The owner's accounts list shows the name someone had before."""
    name = f"{MATRIX}/profile/{friend_id}/displayname"

    def renamed():
        users = expect(call("GET", INVITE + "/admin/users", token=owner), 200)["users"]
        return next(u for u in users if u["user_id"] == friend_id).get("renamed")

    expect(call("PUT", name, {"displayname": "Lena K."}, friend), 200)
    expect(call("PUT", name, {"displayname": "Lena K."}, friend), 200)
    # A first name, in place of the username, is not reported.
    assert renamed() is None, renamed()
    expect(call("PUT", name, {"displayname": "Lenche"}, friend), 200)
    after = renamed()
    assert after and after["from"] == "Lena K." and after["at"] > 0, after
    expect(call("GET", name, token=friend), 200, displayname="Lenche")
    # Nobody may rename someone else.
    other = f"{MATRIX}/profile/@boss:chat.test/displayname"
    assert call("PUT", other, {"displayname": "x"}, friend)[0] in (400, 403)


def moderation_test(owner, friend, friend_id):
    """Only the owner bans or mutes for a while, and it ends by itself.
    The test server counts a "minute" as one second."""
    path = INVITE + "/moderation"
    room = expect(
        call("POST", MATRIX + "/createRoom", {"name": "Rules", "invite": [friend_id]}, owner),
        200,
    )["room_id"]
    expect(call("POST", f"{MATRIX}/rooms/{room}/join", {}, friend), 200)

    def say(token, text):
        txn = f"m{time.time_ns()}"
        body = {"msgtype": "m.text", "body": text}
        return call("PUT", f"{MATRIX}/rooms/{room}/send/m.room.message/{txn}", body, token)

    def membership():
        state = call("GET", f"{MATRIX}/rooms/{room}/state/m.room.member/{friend_id}", token=owner)
        return expect(state, 200)["membership"]

    expect(say(friend, "hello"), 200)
    expect(call("GET", path), 401)
    expect(call("GET", path, token=friend), 200, mine=[])
    mute = {"room_id": room, "user_id": friend_id, "kind": "mute", "minutes": 600}
    # Not for friends, not for the owner as a target, not for nonsense.
    expect(call("POST", path, mute, friend), 403)
    expect(call("POST", path, dict(mute, user_id="@boss:chat.test"), owner), 400)
    expect(call("POST", path, dict(mute, minutes=0), owner), 400)
    expect(call("POST", path, dict(mute, kind="shout"), owner), 400)
    expect(call("POST", path, dict(mute, user_id="@ghost:chat.test"), owner), 404)

    muted = expect(call("POST", path, mute, owner), 200)
    assert muted["kind"] == "mute" and muted["room_name"] == "Rules", muted
    expect(say(friend, "still here"), 403, errcode="DARTCHAT_MUTED")
    expect(say(owner, "quiet now"), 200)
    mine = expect(call("GET", path, token=friend), 200)
    assert [e["id"] for e in mine["mine"]] == [muted["id"]] and "all" not in mine, mine
    assert "user_id" not in mine["mine"][0], mine
    everything = expect(call("GET", path, token=owner), 200)["all"]
    assert [e["user_id"] for e in everything] == [friend_id], everything
    expect(call("DELETE", f"{path}/{muted['id']}", token=friend), 403)
    expect(call("DELETE", f"{path}/{muted['id']}", token=owner), 200)
    expect(say(friend, "back"), 200)

    # A short mute ends by itself.
    expect(call("POST", path, dict(mute, minutes=3), owner), 200)
    expect(say(friend, "no"), 403)
    for _ in range(20):
        if say(friend, "yes")[0] == 200:
            break
        time.sleep(1)
    expect(say(friend, "yes again"), 200)

    # A ban is lifted on time and the person is invited back.
    ban = expect(call("POST", path, dict(mute, kind="ban", minutes=4), owner), 200)
    assert membership() == "ban", membership()
    expect(say(friend, "banned"), 403)
    assert [e["id"] for e in expect(call("GET", path, token=friend), 200)["mine"]] == [ban["id"]]
    for _ in range(30):
        if membership() == "invite":
            break
        time.sleep(1)
    assert membership() == "invite", membership()
    expect(call("GET", path, token=friend), 200, mine=[])
    expect(call("POST", f"{MATRIX}/rooms/{room}/join", {}, friend), 200)

    # The owner can lift a ban early.
    ban = expect(call("POST", path, dict(mute, kind="ban", minutes=600), owner), 200)
    assert membership() == "ban", membership()
    expect(call("DELETE", f"{path}/{ban['id']}", token=owner), 200)
    assert membership() == "invite", membership()


def looks_test():
    """The look catalog and its files come straight from the front door,
    with no sign-in, and are cheap to check again."""
    import hashlib
    request = urllib.request.Request(BASE + "/_dartchat/v1/looks",
                                     headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.headers.get("Content-Encoding") == "gzip", response.headers
        assert response.headers.get("Access-Control-Allow-Origin") == "*"
        etag = response.headers["ETag"]
        import gzip
        catalog = json.loads(gzip.decompress(response.read()))
    assert catalog["format"] == 1
    assert any(t["id"] == "standard" for t in catalog["themes"]), catalog["themes"]
    assert catalog["stickers"] and catalog["gifs"] and catalog["fonts"]
    assert catalog["features"]["stickers"] is True
    again = urllib.request.Request(BASE + "/_dartchat/v1/looks",
                                   headers={"If-None-Match": etag})
    try:
        urllib.request.urlopen(again, timeout=30)
        raise AssertionError("an unchanged catalog should answer 304")
    except urllib.error.HTTPError as error:
        assert error.code == 304, error.code
    picture = catalog["stickers"][0]["items"][0]["file"]
    with urllib.request.urlopen(BASE + "/_dartchat/v1/looks/" + picture["path"],
                                timeout=30) as response:
        data = response.read()
        assert "immutable" in response.headers.get("Cache-Control", "")
    assert hashlib.sha256(data).hexdigest() == picture["sha256"]


def main():
    server = HTTPServer(("127.0.0.1", 6190), FakeResend)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    expect(call("GET", "/_matrix/client/versions"), 200)
    expect(call("GET", INVITE + "/status"), 200, invites=True, email=True)

    # The first account is made with the one-time token and becomes the owner.
    register = {"username": "boss", "password": "boss-password-1"}
    session = expect(call("POST", MATRIX + "/register", register), 401)["session"]
    register["auth"] = {
        "type": "m.login.registration_token",
        "token": FIRST_TOKEN,
        "session": session,
    }
    owner = expect(call("POST", MATRIX + "/register", register), 200)["access_token"]

    expect(call("GET", INVITE + "/admin/invites"), 401)
    expect(call("GET", INVITE + "/admin/invites", token=owner), 200, invites=[])
    expect(call("POST", INVITE + "/admin/invites", {"email": "nope"}, owner), 400)

    invite = expect(
        call("POST", INVITE + "/admin/invites", {"email": "Ana@Example.com"}, owner),
        200,
        email="ana@example.com",
        email_sent=True,
    )
    assert mails[-1]["to"] == ["ana@example.com"], mails[-1]
    assert mails[-1]["auth"] == "Bearer re_smoke_test", mails[-1]
    assert invite["code"] in mails[-1]["text"], mails[-1]

    unsent = expect(
        call("POST", INVITE + "/admin/invites", {"email": "fail@example.com"}, owner),
        200,
        email_sent=False,
    )

    def join(code, username, password="ana-password-1"):
        body = {"code": code, "username": username, "password": password}
        return call("POST", INVITE + "/register", body)

    expect(join("AAAA-BBBB", "ana"), 403, errcode="DARTCHAT_INVALID_CODE")
    expect(join(invite["code"], "boss"), 400, errcode="M_USER_IN_USE")
    expect(join(invite["code"], "ana", "short"), 400, errcode="M_WEAK_PASSWORD")
    expect(join(invite["code"].lower().replace("-", " "), "ana"), 200)
    expect(join(invite["code"], "ana2"), 403, errcode="DARTCHAT_INVALID_CODE")
    friend = expect(login("ana", "ana-password-1"), 200)["access_token"]

    expect(call("GET", INVITE + "/admin/invites", token=friend), 403)
    expect(call("DELETE", f"{INVITE}/admin/invites/{invite['id']}", token=owner), 409)
    revoked = expect(
        call("DELETE", f"{INVITE}/admin/invites/{unsent['id']}", token=owner), 200
    )
    assert revoked["status"] == "revoked", revoked
    expect(join(unsent["code"], "eve"), 403)

    users = expect(call("GET", INVITE + "/admin/users", token=owner), 200)["users"]
    assert {u["user_id"] for u in users} == {"@boss:chat.test", "@ana:chat.test"}, users
    deactivate = INVITE + "/admin/users/deactivate"
    expect(call("POST", deactivate, {"user_id": "@boss:chat.test"}, owner), 400)
    expect(call("POST", deactivate, {"user_id": "@ghost:chat.test"}, owner), 400)
    expect(call("POST", deactivate, {"user_id": "@ana:chat.test"}, owner), 200)
    expect(login("ana", "ana-password-1"), 403, errcode="M_USER_DEACTIVATED")

    circles_test(owner, join)
    scheduled_test(owner)
    looks_test()

    # A friend for the group bot test (tests/bot_e2e.py), which runs next.
    lena = expect(
        call("POST", INVITE + "/admin/invites", {"email": "lena@example.com"}, owner), 200
    )
    expect(join(lena["code"], "lena", "lena-password-1"), 200)
    lena_token = expect(login("lena", "lena-password-1"), 200)["access_token"]
    moderation_test(owner, lena_token, "@lena:chat.test")
    renamed_test(owner, lena_token, "@lena:chat.test")
    bot = expect(
        call("POST", INVITE + "/admin/invites", {"email": "bot@example.com"}, owner), 200
    )
    expect(join(bot["code"], "dartbot"), 400, errcode="M_USER_IN_USE")

    # Wrong codes are cut off.
    for attempt in range(12):
        result = join(f"ZZZZ-ZZZ{attempt % 10}", "x", "x-password-1")
    expect(result, 429, errcode="M_LIMIT_EXCEEDED")
    print("Smoke test passed")


if __name__ == "__main__":
    main()
