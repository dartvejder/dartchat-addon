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

    # Wrong codes are cut off.
    for attempt in range(12):
        result = join(f"ZZZZ-ZZZ{attempt % 10}", "x", "x-password-1")
    expect(result, 429, errcode="M_LIMIT_EXCEEDED")
    print("Smoke test passed")


if __name__ == "__main__":
    main()
