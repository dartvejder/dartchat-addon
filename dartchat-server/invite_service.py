#!/usr/bin/env python3
"""Dart Chat invitation service.

Lets the server owner invite friends by email from inside the app. Each
invitation holds one single-use code. A friend creates an account with that
code; the service then registers the account on the chat server using a
registration token that never leaves this machine.

It also gives the owner two account tools: list the accounts and deactivate
one. Owner requests carry the owner's own chat sign-in token, and the service
checks with the chat server that the token belongs to a server admin.

Uses only the Python standard library.
"""

import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/_dartchat/v1"
SERVER_NAME = os.environ.get("DARTCHAT_SERVER_NAME", "")
HOMESERVER = os.environ.get("DARTCHAT_HOMESERVER", "http://127.0.0.1:6166")
REGISTRATION_TOKEN = os.environ.get("DARTCHAT_REGISTRATION_TOKEN", "")
RESEND_KEY = os.environ.get("DARTCHAT_RESEND_KEY", "")
RESEND_BASE = os.environ.get("DARTCHAT_RESEND_BASE", "https://api.resend.com")
EMAIL_FROM = os.environ.get("DARTCHAT_INVITE_FROM", "")
APP_LINK = os.environ.get("DARTCHAT_APP_LINK", "")
PORT = int(os.environ.get("DARTCHAT_INVITE_PORT", "6168"))
STORE = os.environ.get("DARTCHAT_INVITE_STORE", "/data/invites.json")

# Letters and digits that are hard to mix up when typed from an email.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Wrong codes allowed per address and in total, per ten minutes.
WINDOW_SECONDS = 600
MAX_WRONG_PER_ADDRESS = 10
MAX_WRONG_TOTAL = 60


def log(message):
    print(f"[dartchat-invite] {message}", flush=True)


class ApiError(Exception):
    def __init__(self, status, errcode, message):
        super().__init__(message)
        self.status = status
        self.errcode = errcode
        self.message = message


def http_json(method, url, body=None, headers=None, timeout=20):
    """Returns (status, parsed JSON or {}) and never raises on HTTP errors."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("User-Agent", "dartchat-invite/1.0")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def chat(method, path, body=None, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        return http_json(
            method, f"{HOMESERVER}/_matrix/client/v3{path}", body, headers
        )
    except (urllib.error.URLError, OSError) as error:
        raise ApiError(
            502, "DARTCHAT_SERVER_DOWN", "The chat server is not answering."
        ) from error


def quote(value):
    return urllib.parse.quote(value, safe="")


def format_code(code):
    return f"{code[:4]}-{code[4:]}"


def normalize_code(text):
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


class Store:
    """Invitations and deactivated accounts, kept in one small JSON file."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"invites": [], "deactivated": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                self.data.update(json.load(handle))

    def save(self):
        temporary = f"{self.path}.tmp"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=1)
        os.replace(temporary, self.path)

    def pending_by_code(self, code):
        for invite in self.data["invites"]:
            if invite["status"] == "pending" and secrets.compare_digest(
                invite["code"], code
            ):
                return invite
        return None

    def new_code(self):
        while True:
            code = "".join(
                secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)
            )
            if not any(i["code"] == code for i in self.data["invites"]):
                return code


class WrongCodeLimiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.failures = []  # (time, address)

    def _recent(self):
        cutoff = time.time() - WINDOW_SECONDS
        self.failures = [f for f in self.failures if f[0] > cutoff]
        return self.failures

    def blocked(self, address):
        with self.lock:
            recent = self._recent()
            mine = sum(1 for f in recent if f[1] == address)
            return mine >= MAX_WRONG_PER_ADDRESS or len(recent) >= MAX_WRONG_TOTAL

    def record(self, address):
        with self.lock:
            self._recent().append((time.time(), address))


store = None
limiter = WrongCodeLimiter()
admin_room_id = None


def require_admin(handler):
    """Returns (user_id, token) when the caller is a server admin."""
    global admin_room_id
    header = handler.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or len(header) < 8:
        raise ApiError(401, "M_MISSING_TOKEN", "Sign in first.")
    token = header[7:].strip()
    status, body = chat("GET", "/account/whoami", token=token)
    if status != 200 or "user_id" not in body:
        raise ApiError(401, "M_UNKNOWN_TOKEN", "Sign in again.")
    user_id = body["user_id"]
    if admin_room_id is None:
        alias = quote(f"#admins:{SERVER_NAME}")
        status, body = chat("GET", f"/directory/room/{alias}", token=token)
        if status != 200 or "room_id" not in body:
            raise ApiError(403, "M_FORBIDDEN", "Only the server owner can do this.")
        admin_room_id = body["room_id"]
    status, body = chat("GET", "/joined_rooms", token=token)
    if status != 200 or admin_room_id not in body.get("joined_rooms", []):
        raise ApiError(403, "M_FORBIDDEN", "Only the server owner can do this.")
    return user_id, token


def admin_command(token, command):
    """Runs a server admin command as the owner and returns the answer text."""
    room = quote(admin_room_id)
    transaction = secrets.token_hex(8)
    status, body = chat(
        "PUT",
        f"/rooms/{room}/send/m.room.message/{transaction}",
        {"msgtype": "m.text", "body": f"!admin {command}"},
        token,
    )
    if status != 200 or "event_id" not in body:
        raise ApiError(502, "DARTCHAT_COMMAND_FAILED", "The server refused the command.")
    event_id = body["event_id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.4)
        status, body = chat(
            "GET", f"/rooms/{room}/messages?dir=b&limit=20", token=token
        )
        for event in body.get("chunk", []):
            content = event.get("content", {})
            reply_to = (
                content.get("m.relates_to", {})
                .get("m.in_reply_to", {})
                .get("event_id")
            )
            if reply_to == event_id:
                return content.get("body", ""), event.get("sender", "")
    raise ApiError(504, "DARTCHAT_COMMAND_FAILED", "The server did not answer in time.")


def display_name(user_id, token):
    status, body = chat("GET", f"/profile/{quote(user_id)}/displayname", token=token)
    name = body.get("displayname") if status == 200 else None
    return name or user_id[1:].split(":")[0]


def default_sender():
    domain = SERVER_NAME.split(".", 1)[1] if SERVER_NAME.count(".") >= 2 else SERVER_NAME
    return f"Dart Chat <invites@{domain}>"


def send_invite_email(email, code, inviter):
    """Returns None when sent, or a short reason when it was not."""
    if not RESEND_KEY:
        return "Email sending is not set up on the server."
    shown = format_code(code)
    steps = [
        "Install the Dart Chat app" + (f": {APP_LINK}" if APP_LINK else "."),
        'Open it and tap "I have an invitation code".',
        f"Enter the code {shown} and pick a username and password.",
    ]
    text = (
        f"{inviter} invited you to Dart Chat, a private chat for friends.\n\n"
        f"Your invitation code: {shown}\n\n"
        + "\n".join(f"{n}. {step}" for n, step in enumerate(steps, 1))
        + "\n\nThe code works once and only for you."
        " If you did not expect this email, you can ignore it.\n"
    )

    def esc(value):
        return (
            value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    html = (
        '<div style="font-family:sans-serif;font-size:16px;line-height:1.5">'
        f"<p>{esc(inviter)} invited you to Dart Chat, a private chat for friends.</p>"
        "<p>Your invitation code:</p>"
        '<p style="font-size:28px;font-weight:bold;letter-spacing:2px">'
        f"{shown}</p><ol>"
        + "".join(f"<li>{esc(step)}</li>" for step in steps)
        + "</ol><p>The code works once and only for you. If you did not expect"
        " this email, you can ignore it.</p></div>"
    )
    try:
        status, body = http_json(
            "POST",
            f"{RESEND_BASE}/emails",
            {
                "from": EMAIL_FROM or default_sender(),
                "to": [email],
                "subject": "Your invitation to Dart Chat",
                "text": text,
                "html": html,
            },
            {"Authorization": f"Bearer {RESEND_KEY}"},
        )
    except (urllib.error.URLError, OSError) as error:
        log(f"Email service unreachable: {error}")
        return "The email service could not be reached."
    if status in (200, 201, 202):
        return None
    reason = str(body.get("message") or f"error {status}")[:200]
    log(f"Email service refused the invitation email: {status} {reason}")
    return f"The email service refused it: {reason}"


def public_invite(invite):
    shown = {
        "id": invite["id"],
        "email": invite["email"],
        "status": invite["status"],
        "created_at": invite["created_at"],
        "email_sent": invite.get("email_sent", False),
    }
    if invite.get("email_error"):
        shown["email_error"] = invite["email_error"]
    if invite["status"] == "pending":
        shown["code"] = format_code(invite["code"])
    if invite.get("used_by"):
        shown["used_by"] = invite["used_by"]
    return shown


# ---- request handlers -------------------------------------------------


def get_status(handler, body):
    return {"invites": True, "email": bool(RESEND_KEY)}


def register(handler, body):
    address = handler.caller_address()
    if limiter.blocked(address):
        raise ApiError(
            429, "M_LIMIT_EXCEEDED", "Too many wrong codes. Try again in ten minutes."
        )
    code = normalize_code(body.get("code", ""))
    username = str(body.get("username", "")).strip().lower()
    password = body.get("password", "")
    if not isinstance(password, str) or len(password) < 8:
        raise ApiError(400, "M_WEAK_PASSWORD", "The password needs at least 8 characters.")
    if not username:
        raise ApiError(400, "M_INVALID_USERNAME", "Pick a username.")
    with store.lock:
        invite = store.pending_by_code(code) if code else None
        if invite is None:
            limiter.record(address)
            raise ApiError(
                403, "DARTCHAT_INVALID_CODE", "That invitation code is not valid."
            )
        request = {"username": username, "password": password, "inhibit_login": True}
        status, answer = chat("POST", "/register", request)
        if status == 401 and "session" in answer:
            request["auth"] = {
                "type": "m.login.registration_token",
                "token": REGISTRATION_TOKEN,
                "session": answer["session"],
            }
            status, answer = chat("POST", "/register", request)
        if status != 200 or "user_id" not in answer:
            if status == 401:
                log("The chat server refused the internal registration token")
                raise ApiError(
                    502, "DARTCHAT_SERVER_DOWN", "The server cannot create accounts right now."
                )
            raise ApiError(
                status if 400 <= status < 500 else 502,
                answer.get("errcode", "M_UNKNOWN"),
                answer.get("error", "The account could not be created."),
            )
        invite.update(
            status="used",
            used_by=answer["user_id"],
            used_at=int(time.time() * 1000),
        )
        store.save()
    log(f"Invitation for {invite['email']} used by {answer['user_id']}")
    return {"user_id": answer["user_id"]}


def list_invites(handler, body):
    require_admin(handler)
    with store.lock:
        invites = [public_invite(i) for i in reversed(store.data["invites"])]
    return {"invites": invites, "email": bool(RESEND_KEY)}


def create_invite(handler, body):
    user_id, token = require_admin(handler)
    email = str(body.get("email", "")).strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.match(email):
        raise ApiError(400, "DARTCHAT_INVALID_EMAIL", "That is not an email address.")
    with store.lock:
        # Inviting the same address again replaces the earlier unused code.
        for old in store.data["invites"]:
            if old["status"] == "pending" and old["email"] == email:
                old["status"] = "revoked"
        invite = {
            "id": secrets.token_hex(8),
            "email": email,
            "code": store.new_code(),
            "status": "pending",
            "created_at": int(time.time() * 1000),
            "created_by": user_id,
            "email_sent": False,
        }
        store.data["invites"].append(invite)
        store.save()
    problem = send_invite_email(email, invite["code"], display_name(user_id, token))
    with store.lock:
        invite["email_sent"] = problem is None
        if problem:
            invite["email_error"] = problem
        store.save()
        return public_invite(invite)


def revoke_invite(handler, body, invite_id):
    require_admin(handler)
    with store.lock:
        for invite in store.data["invites"]:
            if invite["id"] == invite_id:
                if invite["status"] == "used":
                    raise ApiError(
                        409, "DARTCHAT_INVITE_USED", "That invitation was already used."
                    )
                invite["status"] = "revoked"
                store.save()
                return public_invite(invite)
    raise ApiError(404, "M_NOT_FOUND", "No such invitation.")


def list_users(handler, body):
    user_id, token = require_admin(handler)
    answer, bot = admin_command(token, "users list-users")
    names = re.findall(r"^(@\S+:\S+)$", answer, re.MULTILINE)
    status, members = chat(
        "GET", f"/rooms/{quote(admin_room_id)}/joined_members", token=token
    )
    admins = set(members.get("joined", {})) if status == 200 else {user_id}
    with store.lock:
        deactivated = set(store.data["deactivated"])
    users = [
        {
            "user_id": name,
            "admin": name in admins,
            "you": name == user_id,
            "deactivated": name in deactivated,
        }
        for name in names
        if name != bot
    ]
    return {"users": users}


def deactivate_user(handler, body):
    user_id, token = require_admin(handler)
    target = str(body.get("user_id", ""))
    if not re.fullmatch(r"@[a-z0-9._=/+-]+:" + re.escape(SERVER_NAME), target):
        raise ApiError(400, "M_INVALID_PARAM", "That is not an account on this server.")
    if target == user_id:
        raise ApiError(400, "DARTCHAT_SELF", "You cannot deactivate your own account here.")
    answer, _ = admin_command(token, f"users deactivate {target}")
    already = "is deactivated" in answer
    if "has been deactivated" not in answer and not already:
        reason = answer.replace("```", "").replace("Command failed with error:", "")
        raise ApiError(400, "DARTCHAT_COMMAND_FAILED", reason.strip()[:200])
    with store.lock:
        if target not in store.data["deactivated"]:
            store.data["deactivated"].append(target)
            store.save()
    log(f"{user_id} deactivated {target}")
    return {"user_id": target, "deactivated": True}


ROUTES = [
    ("GET", re.compile(r"/status"), get_status),
    ("POST", re.compile(r"/register"), register),
    ("GET", re.compile(r"/admin/invites"), list_invites),
    ("POST", re.compile(r"/admin/invites"), create_invite),
    ("DELETE", re.compile(r"/admin/invites/([0-9a-f]{16})"), revoke_invite),
    ("GET", re.compile(r"/admin/users"), list_users),
    ("POST", re.compile(r"/admin/users/deactivate"), deactivate_user),
]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "dartchat-invite"

    def log_message(self, *args):
        pass  # Requests are not logged; they name email addresses.

    def caller_address(self):
        forwarded = self.headers.get("CF-Connecting-IP") or self.headers.get(
            "X-Forwarded-For", ""
        ).split(",")[0]
        return forwarded.strip() or self.client_address[0]

    def answer(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 16384:
                self.close_connection = True
                raise ApiError(413, "M_TOO_LARGE", "Request too large.")
            raw = self.rfile.read(length) if length else b""
            if not path.startswith(PREFIX + "/"):
                raise ApiError(404, "M_UNRECOGNIZED", "Not found.")
            sub = path[len(PREFIX):]
            found = False
            for method, pattern, function in ROUTES:
                match = pattern.fullmatch(sub)
                if not match:
                    continue
                found = True
                if method != self.command:
                    continue
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = None
                if not isinstance(body, dict):
                    raise ApiError(400, "M_NOT_JSON", "Send a JSON object.")
                self.answer(200, function(self, body, *match.groups()))
                return
            if found:
                raise ApiError(405, "M_UNRECOGNIZED", "Method not allowed.")
            raise ApiError(404, "M_UNRECOGNIZED", "Not found.")
        except ApiError as error:
            self.answer(error.status, {"errcode": error.errcode, "error": error.message})
        except Exception as error:  # Keep serving whatever happens.
            log(f"Unexpected error on {self.command} {path}: {error!r}")
            self.answer(500, {"errcode": "M_UNKNOWN", "error": "Something went wrong."})

    do_GET = do_POST = do_DELETE = handle_request


def main():
    global store
    if not SERVER_NAME or not REGISTRATION_TOKEN:
        log("FATAL: server name or registration token missing")
        return 78
    store = Store(STORE)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    if RESEND_KEY:
        log(f"Ready. Invitation emails are sent from {EMAIL_FROM or default_sender()}")
    else:
        log("Ready. No email key set, so invitation codes are only shown in the app")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
