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
        body = {"invite": [f"@{name}:chat.test" for name in invite], "preset": "private_chat"}
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
    expect(call("DELETE", f"{INVITE}/admin/circles/{work}", token=owner), 200)
    expect(profile(sue, "wes"), 200)  # they share a chat now
    expect(create_room(sue, ["wes"]), 404)

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

    # A friend for the group bot test (tests/bot_e2e.py), which runs next.
    lena = expect(
        call("POST", INVITE + "/admin/invites", {"email": "lena@example.com"}, owner), 200
    )
    expect(join(lena["code"], "lena", "lena-password-1"), 200)
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
