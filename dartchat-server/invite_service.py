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

import base64
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
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
# The push gateway on this machine, used to tell an owner's phone that
# someone they invited has joined.
PUSH_URL = os.environ.get(
    "DARTCHAT_PUSH_URL", "http://127.0.0.1:5000/_matrix/push/v1/notify"
)
# The group-rules bot (group_bot/). It joins only groups the owner chose, and
# group members must be able to reach its encryption keys whatever their circle.
BOT_USERNAME = "dartbot"
BOT_USER = f"@{BOT_USERNAME}:{SERVER_NAME}"
# Set when the owner chose a shared registration token by hand; direct
# sign-up with that token then stays possible.
SHARED_TOKEN = os.environ.get("DARTCHAT_SHARED_TOKEN", "") == "1"

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
        self.data = {"invites": [], "deactivated": [], "circles": [], "owners": []}
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
    with store.lock:
        if user_id not in store.data["owners"]:
            store.data["owners"].append(user_id)
            store.save()
    remember_phones(user_id, token)
    return user_id, token


def remember_phones(user_id, token):
    """Notes which phones an owner is signed in on, for the "joined" notice.

    Only the owner's own sign-in can list their phones, so this is refreshed
    whenever the owner uses the invitation screen.
    """
    try:
        status, body = chat("GET", "/pushers", token=token)
    except ApiError:
        return
    if status != 200:
        return
    phones = [
        {"pushkey": pusher["pushkey"], "app_id": pusher.get("app_id", "")}
        for pusher in body.get("pushers") or []
        if isinstance(pusher, dict)
        and pusher.get("kind") == "http"
        and pusher.get("pushkey")
    ]
    with store.lock:
        known = store.data.setdefault("owner_phones", {})
        if known.get(user_id) != phones:
            known[user_id] = phones
            store.save()


def notify_joined(inviter):
    """Wakes the inviting owner's phones with a "someone joined" notice."""
    notify_owner("joined", inviter)


def notify_owner(what, owner=None):
    """Wakes an owner's phones with a "joined" or "renamed" notice.

    Like every wake-up, it names nobody: the phone shows a fixed text and
    the owner looks up who it was in the app. Without [owner], or when that
    owner has no known phone, every owner's phones are woken.
    """
    inviter = owner
    with store.lock:
        known = store.data.get("owner_phones", {})
        phones = list(known.get(inviter) or [])
        if not phones:
            phones = [phone for owner in known.values() for phone in owner]
    if not phones:
        return

    def send():
        try:
            status, _ = http_json(
                "POST",
                PUSH_URL,
                {
                    "notification": {
                        "kind": what,
                        "counts": {"unread": 1},
                        "devices": phones,
                    }
                },
            )
            if status != 200:
                log(f"The {what} notice was not sent (status {status})")
        except (urllib.error.URLError, OSError) as error:
            log(f"The {what} notice was not sent: {error}")

    threading.Thread(target=send, daemon=True).start()


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
    shown["circles"] = invite.get("circles", [])
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
    if username == BOT_USERNAME:
        raise ApiError(400, "M_USER_IN_USE", "That username is taken.")
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
        for circle in store.data["circles"]:
            if circle["id"] in invite.get("circles", []):
                if answer["user_id"] not in circle["members"]:
                    circle["members"].append(answer["user_id"])
        store.save()
    log(f"Invitation for {invite['email']} used by {answer['user_id']}")
    notify_joined(invite.get("created_by"))
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
    wanted = body.get("circles", [])
    if not isinstance(wanted, list):
        raise ApiError(400, "M_INVALID_PARAM", "circles must be a list.")
    with store.lock:
        known = {circle["id"] for circle in store.data["circles"]}
        if any(circle_id not in known for circle_id in wanted):
            raise ApiError(400, "DARTCHAT_NO_CIRCLE", "That circle does not exist.")
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
            "circles": list(wanted),
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
        renames = dict(store.data.get("renames", {}))
    users = [
        {
            "user_id": name,
            "admin": name in admins,
            "you": name == user_id,
            "deactivated": name in deactivated,
            # The name this person showed before they last changed it.
            **({"renamed": renames[name]} if name in renames else {}),
        }
        for name in names
        if name not in (bot, BOT_USER, f"@{LINK_SENDER}:{SERVER_NAME}")
    ]
    return {"users": users}


def current_display_name(user_id, token):
    try:
        status, body = chat(
            "GET", f"/profile/{quote(user_id)}/displayname", token=token
        )
    except ApiError:
        return ""
    name = body.get("displayname") if status == 200 else ""
    return name.strip() if isinstance(name, str) else ""


def note_rename(user_id, before, after):
    """Remembers a changed display name and tells the owners' phones.

    The first name someone sets (until then it is empty or their username),
    and a change by an owner, are not news. Only the owner's accounts list
    ever shows the old name.
    """
    after = after.strip()
    username = user_id[1:].split(":")[0]
    if not before or before == after or before.lower() == username.lower():
        return
    with store.lock:
        if user_id in store.data["owners"]:
            return
        store.data.setdefault("renames", {})[user_id] = {
            "from": before[:200],
            "at": int(time.time() * 1000),
        }
        store.save()
    notify_owner("renamed")


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



# ---- circles ------------------------------------------------------------
#
# A circle is a set of accounts that can find, see and message each other.
# The owner sees everyone and everyone sees the owner. While no circle
# exists, everyone can see everyone.


def clean_circle_name(body):
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 40:
        raise ApiError(400, "M_INVALID_PARAM", "Give the circle a name of up to 40 characters.")
    return name


def clean_members(body):
    members = body.get("members", [])
    pattern = r"@[a-z0-9._=/+-]+:" + re.escape(SERVER_NAME)
    if not isinstance(members, list) or not all(
        isinstance(m, str) and re.fullmatch(pattern, m) for m in members
    ):
        raise ApiError(400, "M_INVALID_PARAM", "members must be accounts on this server.")
    return sorted(set(members))


def list_circles(handler, body):
    require_admin(handler)
    with store.lock:
        return {"circles": json.loads(json.dumps(store.data["circles"]))}


def create_circle(handler, body):
    require_admin(handler)
    name = clean_circle_name(body)
    members = clean_members(body)
    with store.lock:
        circle = {"id": secrets.token_hex(8), "name": name, "members": members}
        store.data["circles"].append(circle)
        store.save()
        return dict(circle)


def update_circle(handler, body, circle_id):
    require_admin(handler)
    with store.lock:
        for circle in store.data["circles"]:
            if circle["id"] == circle_id:
                if "name" in body:
                    circle["name"] = clean_circle_name(body)
                if "members" in body:
                    circle["members"] = clean_members(body)
                store.save()
                return dict(circle)
    raise ApiError(404, "M_NOT_FOUND", "No such circle.")


def delete_circle(handler, body, circle_id):
    require_admin(handler)
    with store.lock:
        before = len(store.data["circles"])
        store.data["circles"] = [
            c for c in store.data["circles"] if c["id"] != circle_id
        ]
        if len(store.data["circles"]) == before:
            raise ApiError(404, "M_NOT_FOUND", "No such circle.")
        for invite in store.data["invites"]:
            if circle_id in invite.get("circles", []):
                invite["circles"].remove(circle_id)
        store.save()
    return {}


# ---- visibility guard -----------------------------------------------------
#
# The front door sends the chat-server requests that could reveal or contact
# another account through here. Each is checked against the circles and then
# passed on to the chat server.

TOKEN_CACHE_SECONDS = 300
ROOM_CACHE_SECONDS = 30
token_cache = {}  # token -> (user id, expires)
room_cache = {}  # user id -> (set of user ids sharing a room, expires)
cache_lock = threading.Lock()

NOT_FOUND = (404, {"errcode": "M_NOT_FOUND", "error": "This account does not exist."})

GUARDED = re.compile(
    r"/_matrix/client/[^/]+/(?:[^/]+/)??(?:"
    r"(?P<search>user_directory/search)"
    r"|profile/(?P<profile>[^/]+)(?:/[^/]*)?"
    r"|presence/(?P<presence>[^/]+)/status"
    r"|(?P<create>createRoom)"
    r"|rooms/(?P<invite_room>[^/]+)/(?P<invite>invite)"
    r"|rooms/(?P<member_room>[^/]+)/state/m\.room\.member/(?P<member>[^/]+)"
    r"|keys/(?P<keys>query|claim)"
    r"|(?P<login>login)"
    r"|(?P<register>register(?:/available)?)"
    r"|rooms/(?P<send_room>[^/]+)/send/(?P<send_type>[^/]+)/[^/]+"
    r")/?"
)


def forward(handler, raw):
    """Passes the request on to the chat server unchanged."""
    connection = http.client.HTTPConnection(
        urllib.parse.urlsplit(HOMESERVER).netloc, timeout=120
    )
    headers = {"User-Agent": handler.headers.get("User-Agent", "dartchat")}
    for name in ("Authorization", "Content-Type", "X-Forwarded-For"):
        if handler.headers.get(name):
            headers[name] = handler.headers[name]
    try:
        connection.request(handler.command, handler.path, raw or None, headers)
        response = connection.getresponse()
        return response.status, response.read(), response.getheader(
            "Content-Type", "application/json"
        )
    except OSError as error:
        raise ApiError(
            502, "DARTCHAT_SERVER_DOWN", "The chat server is not answering."
        ) from error
    finally:
        connection.close()


def caller(handler):
    """The account making a chat-server request."""
    header = handler.headers.get("Authorization", "")
    token = header[7:].strip() if header.startswith("Bearer ") else ""
    if not token:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
        token = (query.get("access_token") or [""])[0]
    if not token:
        raise ApiError(401, "M_MISSING_TOKEN", "Missing access token.")
    now = time.time()
    with cache_lock:
        cached = token_cache.get(token)
        if cached and cached[1] > now:
            return cached[0], token
    status, body = chat("GET", "/account/whoami", token=token)
    if status != 200 or "user_id" not in body:
        raise ApiError(401, "M_UNKNOWN_TOKEN", "Unknown access token.")
    with cache_lock:
        if len(token_cache) > 5000:
            token_cache.clear()
        token_cache[token] = (body["user_id"], now + TOKEN_CACHE_SECONDS)
    return body["user_id"], token


def room_mates(user_id, token):
    """Everyone who is in, or invited to, a chat that [user_id] is in."""
    now = time.time()
    with cache_lock:
        cached = room_cache.get(user_id)
        if cached and cached[1] > now:
            return cached[0]
    mates = set()
    status, body = chat("GET", "/joined_rooms", token=token)
    for room_id in body.get("joined_rooms", []) if status == 200 else []:
        status, members = chat("GET", f"/rooms/{quote(room_id)}/members", token=token)
        for event in members.get("chunk", []) if status == 200 else []:
            if event.get("content", {}).get("membership") in ("join", "invite"):
                mates.add(event.get("state_key"))
    with cache_lock:
        room_cache[user_id] = (mates, now + ROOM_CACHE_SECONDS)
    return mates


def in_same_circle(user_id, target):
    """Whether the circles alone let [user_id] see [target]."""
    with store.lock:
        circles = store.data["circles"]
        owners = store.data["owners"]
        if not circles or user_id in owners or target in owners:
            return True
        return any(
            user_id in circle["members"] and target in circle["members"]
            for circle in circles
        )


def may_contact(user_id, target):
    """Whether [user_id] may start a chat with [target] or invite them."""
    return (
        user_id == target
        or not target.endswith(":" + SERVER_NAME)
        or in_same_circle(user_id, target)
    )


def may_see(user_id, token, target):
    """Whether [user_id] may see [target]: allowed to contact them, or they
    already share a chat."""
    if BOT_USER in (user_id, target):
        return True
    return may_contact(user_id, target) or target in room_mates(user_id, token)


owner_cache = {}


def is_owner(user_id, token):
    """Whether the caller is a server owner (a member of the admin room)."""
    global admin_room_id
    with store.lock:
        if user_id in store.data["owners"]:
            return True
    now = time.time()
    with cache_lock:
        cached = owner_cache.get(user_id)
        if cached and cached[1] > now:
            return cached[0]
    if admin_room_id is None:
        alias = quote(f"#admins:{SERVER_NAME}")
        status, body = chat("GET", f"/directory/room/{alias}", token=token)
        if status == 200 and "room_id" in body:
            admin_room_id = body["room_id"]
    owner = False
    if admin_room_id is not None:
        status, body = chat("GET", "/joined_rooms", token=token)
        owner = status == 200 and admin_room_id in body.get("joined_rooms", [])
    with cache_lock:
        if len(owner_cache) > 5000:
            owner_cache.clear()
        owner_cache[user_id] = (owner, now + ROOM_CACHE_SECONDS)
    if owner:
        with store.lock:
            if user_id not in store.data["owners"]:
                store.data["owners"].append(user_id)
                store.save()
    return owner


def runs_groups(user_id, token):
    """Whether [user_id] may create groups and add people to them."""
    return user_id == BOT_USER or is_owner(user_id, token)


def stays_private(room_id, token, target):
    """Whether inviting [target] keeps the chat a conversation between two
    people. Everyone may do that; only the owner makes anything larger."""
    status, body = chat("GET", f"/rooms/{quote(room_id)}/members", token=token)
    if status != 200:
        return False
    people = {
        event.get("state_key")
        for event in body.get("chunk", [])
        if event.get("content", {}).get("membership") in ("join", "invite")
    }
    people.add(target)
    return len(people) <= 2


OWNER_ONLY = (
    403,
    {
        "errcode": "M_FORBIDDEN",
        "error": "Only the owner creates groups and adds people to them.",
    },
)


def sign_up_open():
    """Direct sign-up on the chat server is only for the very first account
    (or for a shared token the owner set by hand)."""
    if SHARED_TOKEN:
        return True
    with store.lock:
        return not store.data["owners"] and not any(
            invite["status"] == "used" for invite in store.data["invites"]
        )


def guard(handler, match, raw):
    """Returns (status, body bytes, content type) for a guarded request."""

    def as_json(status, body):
        return status, json.dumps(body).encode(), "application/json"

    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = None
    method = handler.command

    if match["login"]:
        status, data, kind = forward(handler, raw)
        if method == "POST" and status in (403, 404):
            # Do not tell strangers which usernames exist.
            try:
                if json.loads(data).get("errcode") == "M_NOT_FOUND":
                    return as_json(
                        403,
                        {"errcode": "M_FORBIDDEN", "error": "Wrong username or password."},
                    )
            except ValueError:
                pass
        return status, data, kind

    if match["register"]:
        if not sign_up_open():
            return as_json(
                403,
                {"errcode": "M_FORBIDDEN", "error": "Sign-up needs an invitation code."},
            )
        return forward(handler, raw)

    if method == "OPTIONS":
        return forward(handler, raw)
    user_id, token = caller(handler)
    remember_owner_session(user_id, token)

    if match["profile"] and method == "GET":
        target = urllib.parse.unquote(match["profile"])
        if not may_see(user_id, token, target):
            return as_json(*NOT_FOUND)
    elif (
        match["profile"]
        and method == "PUT"
        and body is not None
        and urllib.parse.unquote(match["profile"]) == user_id
        and handler.path.split("?")[0].rstrip("/").endswith("/displayname")
        and isinstance(body.get("displayname"), str)
    ):
        before = current_display_name(user_id, token)
        status, data, kind = forward(handler, raw)
        if status == 200:
            note_rename(user_id, before, body["displayname"])
        return status, data, kind
    elif match["presence"] and method == "GET":
        target = urllib.parse.unquote(match["presence"])
        if not may_see(user_id, token, target):
            return as_json(*NOT_FOUND)
    elif match["create"] and method == "POST" and body is not None:
        invited = body.get("invite", [])
        if isinstance(invited, list) and not all(
            isinstance(t, str) and may_contact(user_id, t) for t in invited
        ):
            return as_json(*NOT_FOUND)
        private = (
            body.get("is_direct") is True
            and isinstance(invited, list)
            and len(invited) <= 1
            and not body.get("invite_3pid")
            and body.get("visibility", "private") == "private"
            and body.get("preset") != "public_chat"
            and not body.get("room_alias_name")
        )
        if not private and not runs_groups(user_id, token):
            return as_json(*OWNER_ONLY)
    elif match["create"] and method == "POST":
        return as_json(400, {"errcode": "M_BAD_JSON", "error": "Not valid JSON."})
    elif match["invite"] and method == "POST":
        target = body.get("user_id") if body is not None else None
        if not isinstance(target, str):
            return as_json(400, {"errcode": "M_BAD_JSON", "error": "Not valid JSON."})
        if not may_contact(user_id, target):
            return as_json(*NOT_FOUND)
        room_id = urllib.parse.unquote(match["invite_room"])
        if not runs_groups(user_id, token) and not stays_private(
            room_id, token, target
        ):
            return as_json(*OWNER_ONLY)
    elif match["member"] and method == "PUT" and body is not None:
        target = urllib.parse.unquote(match["member"])
        if body.get("membership") == "invite":
            if not may_contact(user_id, target):
                return as_json(*NOT_FOUND)
            room_id = urllib.parse.unquote(match["member_room"])
            if not runs_groups(user_id, token) and not stays_private(
                room_id, token, target
            ):
                return as_json(*OWNER_ONLY)
    elif match["keys"] and method == "POST" and body is not None:
        field = "device_keys" if match["keys"] == "query" else "one_time_keys"
        wanted = body.get(field)
        if isinstance(wanted, dict):
            allowed = {
                target: value
                for target, value in wanted.items()
                if may_see(user_id, token, target)
            }
            if len(allowed) != len(wanted):
                body[field] = allowed
                raw = json.dumps(body).encode()
    elif match["send_room"] and method == "PUT":
        until = muted_until(user_id, urllib.parse.unquote(match["send_room"]))
        if until:
            return as_json(
                403,
                {
                    "errcode": "DARTCHAT_MUTED",
                    "error": f"You are muted in this group until {describe_until(until)}.",
                    "until": until,
                },
            )
        status, data, kind = forward(handler, raw)
        if status == 200:
            room_id = urllib.parse.unquote(match["send_room"])
            event_type = urllib.parse.unquote(match["send_type"])
            threading.Thread(
                target=count_message,
                args=(user_id, token, room_id, event_type, body, data),
                daemon=True,
            ).start()
        return status, data, kind
    elif match["search"] and method == "POST":
        status, data, kind = forward(handler, raw)
        if status != 200:
            return status, data, kind
        try:
            answer = json.loads(data)
            answer["results"] = [
                result
                for result in answer.get("results", [])
                if result.get("user_id") != BOT_USER
                and may_see(user_id, token, result.get("user_id", ""))
            ]
            return as_json(200, answer)
        except (ValueError, AttributeError):
            return as_json(200, {"results": [], "limited": False})

    return forward(handler, raw)


# ---- message counts --------------------------------------------------------
#
# For the owner's daily storage email: how many messages were sent each day in
# each group and, as one total, in private chats. Messages are end-to-end
# encrypted, so only the count is kept, never anything about the content.
# Edits are not counted; reactions are counted on their own.

COUNTS_FILE = os.environ.get("DARTCHAT_MESSAGE_COUNTS", "/data/message_counts.json")
TIMEZONE = os.environ.get("DARTCHAT_TIMEZONE", "") or "Europe/Belgrade"
ROOM_KIND_SECONDS = 3600
KEEP_DAILY_COUNTS_DAYS = 400
counts_lock = threading.Lock()
room_kinds = {}  # room id -> (is group, name, expires)
recent_events = []  # event ids already counted (a client may send twice)


def counts_today():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(TIMEZONE)
    except Exception:
        zone = ZoneInfo("Europe/Belgrade")
    return datetime.now(zone).date()


def message_kind(event_type, body):
    """'message', 'reaction', or None for things that are not counted."""
    relates = body.get("m.relates_to") if isinstance(body, dict) else None
    relation = relates.get("rel_type") if isinstance(relates, dict) else None
    if event_type == "m.reaction" or relation == "m.annotation":
        return "reaction"
    if event_type not in ("m.room.encrypted", "m.room.message", "m.sticker"):
        return None
    if relation == "m.replace":
        return None  # an edit of a message already counted
    return "message"


def room_kind(room_id, token):
    """(is group, name). A group has a name or more than two people."""
    now = time.time()
    with counts_lock:
        cached = room_kinds.get(room_id)
        if cached and cached[2] > now:
            return cached[0], cached[1]
    status, body = chat("GET", f"/rooms/{quote(room_id)}/state/m.room.name", token=token)
    name = str(body.get("name") or "").strip() if status == 200 else ""
    status, body = chat("GET", f"/rooms/{quote(room_id)}/joined_members", token=token)
    people = len(body.get("joined", {})) if status == 200 else 0
    group = bool(name) or people > 2
    with counts_lock:
        room_kinds[room_id] = (group, name, now + ROOM_KIND_SECONDS)
    return group, name


def load_counts():
    try:
        with open(COUNTS_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("days", {})
    data.setdefault("rooms", {})
    return data


def save_counts(data):
    tmp = COUNTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
    os.replace(tmp, COUNTS_FILE)


def fold_old_days(data, today):
    """Days older than KEEP_DAILY_COUNTS_DAYS are merged into the first day of
    their month, so the file stays small while monthly and yearly totals stay
    right."""
    from datetime import timedelta

    limit = (today - timedelta(days=KEEP_DAILY_COUNTS_DAYS)).isoformat()
    days = data["days"]
    for day in sorted(d for d in days if d < limit and not d.endswith("-01")):
        target = days.setdefault(day[:8] + "01", {})
        for key, value in days.pop(day).items():
            if isinstance(value, dict):
                into = target.setdefault(key, {})
                for room, n in value.items():
                    into[room] = into.get(room, 0) + n
            else:
                target[key] = target.get(key, 0) + value


def count_message(user_id, token, room_id, event_type, body, answer):
    """Adds one sent message (or reaction) to today's counts."""
    try:
        if user_id == BOT_USER:
            return
        kind = message_kind(event_type, body)
        if kind is None:
            return
        try:
            event_id = json.loads(answer).get("event_id")
        except (ValueError, AttributeError):
            event_id = None
        with counts_lock:
            if event_id:
                if event_id in recent_events:
                    return
                recent_events.append(event_id)
                del recent_events[:-500]
        group, name = room_kind(room_id, token)
        today = counts_today()
        with counts_lock:
            data = load_counts()
            day = data["days"].setdefault(today.isoformat(), {})
            if kind == "reaction":
                day["reactions"] = day.get("reactions", 0) + 1
            elif group:
                groups = day.setdefault("groups", {})
                groups[room_id] = groups.get(room_id, 0) + 1
                if name:
                    data["rooms"][room_id] = name
            else:
                day["private"] = day.get("private", 0) + 1
            fold_old_days(data, today)
            save_counts(data)
    except Exception as error:  # Counting must never get in the way.
        log(f"Could not count a message: {error!r}")


# ---- scheduled messages ----------------------------------------------------
#
# A phone can hand over a message to be sent later. The phone encrypts it
# first, so what waits here is ciphertext: the server never sees the text.
# To send it at the right time in the sender's name, the sender's sign-in
# for that phone is kept next to it until the message has gone out.

SCHEDULE_FILE = os.environ.get("DARTCHAT_SCHEDULE_STORE", "/data/scheduled.json")
SCHEDULE_MAX_PER_USER = 50
SCHEDULE_MAX_AHEAD_MS = 366 * 24 * 3600 * 1000
SCHEDULE_GIVE_UP_SECONDS = 6 * 3600
SCHEDULE_KEEP_FAILED_SECONDS = 7 * 24 * 3600

schedule_lock = threading.Lock()
schedule = None  # list of entries, loaded on first use


def load_schedule():
    global schedule
    if schedule is None:
        schedule = []
        try:
            with open(SCHEDULE_FILE, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, list):
                schedule = loaded
        except (OSError, ValueError):
            pass
    return schedule


def save_schedule():
    temporary = f"{SCHEDULE_FILE}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(schedule, handle)
    os.replace(temporary, SCHEDULE_FILE)


def public_scheduled(entry):
    return {
        "id": entry["id"],
        "room_id": entry["room_id"],
        "send_at": entry["send_at"],
        "status": entry["status"],
    }


def list_scheduled(handler, body):
    user_id, _ = caller(handler)
    with schedule_lock:
        mine = [public_scheduled(e) for e in load_schedule() if e["user_id"] == user_id]
    return {"scheduled": sorted(mine, key=lambda e: e["send_at"])}


def create_scheduled(handler, body):
    user_id, token = caller(handler)
    room_id = body.get("room_id")
    content = body.get("content")
    send_at = body.get("send_at")
    now_ms = int(time.time() * 1000)
    if not isinstance(room_id, str) or not room_id.startswith("!"):
        raise ApiError(400, "M_INVALID_PARAM", "room_id is missing.")
    if isinstance(send_at, bool) or not isinstance(send_at, int):
        raise ApiError(400, "M_INVALID_PARAM", "send_at must be a time in milliseconds.")
    if send_at > now_ms + SCHEDULE_MAX_AHEAD_MS:
        raise ApiError(400, "M_INVALID_PARAM", "That is too far in the future.")
    # Only encrypted messages wait here; the text never reaches the server.
    if (
        not isinstance(content, dict)
        or content.get("algorithm") != "m.megolm.v1.aes-sha2"
        or not isinstance(content.get("ciphertext"), str)
        or not isinstance(content.get("session_id"), str)
    ):
        raise ApiError(400, "DARTCHAT_NOT_ENCRYPTED", "Only encrypted messages can be scheduled.")
    status, joined = chat("GET", "/joined_rooms", token=token)
    if status != 200 or room_id not in joined.get("joined_rooms", []):
        raise ApiError(403, "M_FORBIDDEN", "You are not in that chat.")
    entry = {
        "id": secrets.token_hex(8),
        "user_id": user_id,
        "token": token,
        "room_id": room_id,
        "content": content,
        "send_at": send_at,
        "status": "waiting",
        "created": int(time.time()),
    }
    with schedule_lock:
        waiting = [
            e for e in load_schedule()
            if e["user_id"] == user_id and e["status"] == "waiting"
        ]
        if len(waiting) >= SCHEDULE_MAX_PER_USER:
            raise ApiError(
                429, "M_LIMIT_EXCEEDED", "Too many scheduled messages. Cancel one first."
            )
        schedule.append(entry)
        save_schedule()
    return public_scheduled(entry)


def cancel_scheduled(handler, body, entry_id):
    user_id, _ = caller(handler)
    with schedule_lock:
        for entry in load_schedule():
            if entry["id"] == entry_id and entry["user_id"] == user_id:
                if entry["status"] == "sending":
                    raise ApiError(409, "DARTCHAT_SENDING", "It is being sent right now.")
                schedule.remove(entry)
                save_schedule()
                return {}
    raise ApiError(404, "M_NOT_FOUND", "No such scheduled message.")


def send_due_messages():
    """Sends what is due. Returns how many were sent."""
    now = time.time()
    with schedule_lock:
        due = [
            e for e in load_schedule()
            if e["status"] == "waiting" and e["send_at"] <= now * 1000
            and e.get("retry_at", 0) <= now
        ]
        for entry in due:
            entry["status"] = "sending"
        # Failed ones are shown to their sender for a week, then forgotten.
        before = len(schedule)
        schedule[:] = [
            e for e in schedule
            if e["status"] != "failed"
            or e.get("failed_at", now) > now - SCHEDULE_KEEP_FAILED_SECONDS
        ]
        if due or len(schedule) != before:
            save_schedule()
    sent = 0
    for entry in due:
        if muted_until(entry["user_id"], entry["room_id"]):
            # A muted person's message waits until the mute is over.
            with schedule_lock:
                entry["status"] = "waiting"
                entry["retry_at"] = int(now) + 60
                save_schedule()
            continue
        try:
            status, answer = chat(
                "PUT",
                f"/rooms/{quote(entry['room_id'])}/send/m.room.encrypted/"
                f"dartchat-scheduled-{entry['id']}",
                entry["content"],
                entry["token"],
            )
        except ApiError:
            status, answer = 502, {}
        with schedule_lock:
            if entry not in schedule:
                continue
            if status == 200:
                schedule.remove(entry)
                sent += 1
            elif status in (401, 403, 404) or (
                now - entry["send_at"] / 1000 > SCHEDULE_GIVE_UP_SECONDS
            ):
                # Signed out on that phone, removed from the chat, or the
                # chat server stayed down for hours.
                entry["status"] = "failed"
                entry["failed_at"] = int(now)
                entry.pop("token", None)
                entry.pop("content", None)
            else:
                entry["status"] = "waiting"
                entry["retry_at"] = int(now) + 30
            save_schedule()
        if status == 200:
            threading.Thread(
                target=count_message,
                args=(
                    entry["user_id"], entry["token"], entry["room_id"],
                    "m.room.encrypted", entry["content"], json.dumps(answer),
                ),
                daemon=True,
            ).start()
    return sent


def schedule_loop():
    with schedule_lock:
        # Anything caught mid-send by a restart goes out again; the fixed
        # transaction name keeps the chat server from sending it twice.
        changed = False
        for entry in load_schedule():
            if entry["status"] == "sending":
                entry["status"] = "waiting"
                changed = True
        if changed:
            save_schedule()
    while True:
        try:
            send_due_messages()
        except Exception as error:  # Keep the clock running whatever happens.
            log(f"Could not send scheduled messages: {error!r}")
        try:
            lift_due()
        except Exception as error:
            log(f"Could not lift a ban: {error!r}")
        time.sleep(5)


# ---- timed bans and mutes ----------------------------------------------------
#
# Only the owner (and the group bot, when the owner tells it to) can ban or
# mute someone in a group for a while. A mute is kept here: while it lasts,
# the front door refuses that person's messages in that group. A ban is a
# real ban on the chat server; when its time is up this service lifts it and
# invites the person back, using the owner's most recent sign-in.

MODERATION_FILE = os.environ.get("DARTCHAT_MODERATION_STORE", "/data/moderation.json")
MODERATION_MAX_MINUTES = 366 * 24 * 60
# A minute, in milliseconds. Tests make it shorter.
MODERATION_MINUTE_MS = int(os.environ.get("DARTCHAT_MODERATION_MINUTE_MS", "60000"))
MODERATION_KEEP_FAILED_SECONDS = 7 * 24 * 3600

moderation_lock = threading.Lock()
moderation = None  # {"entries": [...], "owner": {"user_id", "token"}}


def load_moderation():
    global moderation
    if moderation is None:
        moderation = {"entries": [], "owner": None}
        try:
            with open(MODERATION_FILE, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), list):
                moderation["entries"] = loaded["entries"]
                if isinstance(loaded.get("owner"), dict):
                    moderation["owner"] = loaded["owner"]
        except (OSError, ValueError):
            pass
    return moderation


def save_moderation():
    temporary = f"{MODERATION_FILE}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(moderation, handle)
    os.replace(temporary, MODERATION_FILE)


def remember_owner_session(user_id, token):
    """Keeps the owner's latest sign-in, so a ban can be lifted on time while
    the owner's phone is off. Never leaves this machine."""
    with store.lock:
        if user_id not in store.data["owners"]:
            return
    with moderation_lock:
        data = load_moderation()
        if data["owner"] != {"user_id": user_id, "token": token}:
            data["owner"] = {"user_id": user_id, "token": token}
            save_moderation()


def muted_until(user_id, room_id):
    """When [user_id]'s mute in [room_id] ends (ms), or None."""
    now = time.time() * 1000
    with moderation_lock:
        for entry in load_moderation()["entries"]:
            if (
                entry["kind"] == "mute"
                and entry["status"] == "active"
                and entry["user_id"] == user_id
                and entry["room_id"] == room_id
                and entry["until"] > now
            ):
                return entry["until"]
    return None


def describe_until(until_ms):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(TIMEZONE)
    except Exception:
        zone = ZoneInfo("Europe/Belgrade")
    local = datetime.fromtimestamp(until_ms / 1000, tz=zone)
    return f"{local.day} {local.strftime('%b %H:%M')}"


def public_moderation(entry, full):
    result = {
        "id": entry["id"],
        "kind": entry["kind"],
        "room_id": entry["room_id"],
        "room_name": entry.get("room_name", ""),
        "until": entry["until"],
        "status": entry["status"],
    }
    if full:
        result["user_id"] = entry["user_id"]
    return result


def list_moderation(handler, body):
    """Everyone sees what applies to themselves; the owner sees everything."""
    user_id, token = caller(handler)
    # The group bot sees everything too, to lift what the owner asks it to.
    owner = user_id == BOT_USER or is_owner(user_id, token)
    with moderation_lock:
        entries = list(load_moderation()["entries"])
    answer = {
        "mine": [
            public_moderation(e, False)
            for e in entries
            if e["user_id"] == user_id
            and e["status"] == "active"
            and (e["kind"] == "ban" or e["until"] > time.time() * 1000)
        ]
    }
    if owner:
        answer["all"] = [public_moderation(e, True) for e in entries]
    return answer


def moderator(handler, body):
    """The owner's (user id, token) for a ban or mute, or an error. The
    group bot may ask on the owner's behalf, naming who told it to."""
    user_id, token = caller(handler)
    if user_id == BOT_USER:
        asked_by = body.get("requested_by")
        with store.lock:
            known = isinstance(asked_by, str) and asked_by in store.data["owners"]
        if not known:
            raise ApiError(403, "M_FORBIDDEN", "Only the owner can do this.")
        with moderation_lock:
            session = load_moderation()["owner"]
        if not session:
            raise ApiError(
                409, "DARTCHAT_OWNER_AWAY",
                "The owner needs to open the app once before the bot can do this.",
            )
        return session["user_id"], session["token"]
    if not is_owner(user_id, token):
        raise ApiError(403, "M_FORBIDDEN", "Only the owner can do this.")
    remember_owner_session(user_id, token)
    return user_id, token


def create_moderation(handler, body):
    owner, token = moderator(handler, body)
    room_id = body.get("room_id")
    target = body.get("user_id")
    kind = body.get("kind")
    minutes = body.get("minutes")
    if (
        not isinstance(room_id, str)
        or not isinstance(target, str)
        or kind not in ("ban", "mute")
        or isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or not 1 <= minutes <= MODERATION_MAX_MINUTES
    ):
        raise ApiError(400, "M_BAD_JSON", "Say who, where, ban or mute, and for how long.")
    with store.lock:
        protected = target == BOT_USER or target in store.data["owners"]
    if protected:
        raise ApiError(400, "DARTCHAT_PROTECTED", "The owner and the bot cannot be banned or muted.")
    status, members = chat("GET", f"/rooms/{quote(room_id)}/members", token=token)
    if status != 200:
        raise ApiError(403, "M_FORBIDDEN", "You are not in that group.")
    people = {
        e.get("state_key"): e.get("content", {}).get("membership")
        for e in members.get("chunk", [])
    }
    if people.get(target) not in ("join", "invite"):
        raise ApiError(404, "M_NOT_FOUND", "That person is not in the group.")
    is_group, room_name = room_kind(room_id, token)
    if not is_group:
        raise ApiError(400, "DARTCHAT_NOT_A_GROUP", "This only works in groups.")
    until = int(time.time() * 1000) + minutes * MODERATION_MINUTE_MS
    entry = {
        "id": secrets.token_hex(8),
        "kind": kind,
        "room_id": room_id,
        "room_name": room_name,
        "user_id": target,
        "until": until,
        "by": owner,
        "status": "active",
    }
    if kind == "ban":
        status, answer = chat(
            "POST",
            f"/rooms/{quote(room_id)}/ban",
            {"user_id": target, "reason": f"Until {describe_until(until)}"},
            token,
        )
        if status != 200:
            raise ApiError(
                403, "DARTCHAT_CANNOT_BAN",
                answer.get("error") or "The owner is not an admin of that group.",
            )
    with moderation_lock:
        data = load_moderation()
        # A new ban or mute replaces an earlier one of the same kind.
        data["entries"][:] = [
            e for e in data["entries"]
            if not (
                e["kind"] == kind and e["room_id"] == room_id and e["user_id"] == target
            )
        ]
        data["entries"].append(entry)
        save_moderation()
    return public_moderation(entry, True)


def lift(entry, token):
    """Ends a ban: the person is unbanned and invited back. True when done."""
    room = quote(entry["room_id"])
    status, answer = chat(
        "POST", f"/rooms/{room}/unban", {"user_id": entry["user_id"]}, token
    )
    if status in (401, 429) or status >= 500:
        return False
    # Already unbanned by hand is fine. Then back in, if they are not already.
    chat("POST", f"/rooms/{room}/invite", {"user_id": entry["user_id"]}, token)
    return True


def cancel_moderation(handler, body, entry_id):
    _, token = moderator(handler, body)
    with moderation_lock:
        entry = next(
            (e for e in load_moderation()["entries"] if e["id"] == entry_id), None
        )
    if entry is None:
        raise ApiError(404, "M_NOT_FOUND", "No such ban or mute.")
    if entry["kind"] == "ban" and not lift(entry, token):
        raise ApiError(502, "DARTCHAT_LIFT_FAILED", "The ban could not be lifted. Try again.")
    with moderation_lock:
        if entry in moderation["entries"]:
            moderation["entries"].remove(entry)
            save_moderation()
    return {}


def lift_due():
    """Ends what has run out. Mutes just stop; bans are lifted."""
    now = time.time()
    with moderation_lock:
        data = load_moderation()
        session = data["owner"]
        due = [
            e for e in data["entries"]
            if e["status"] == "active" and e["until"] <= now * 1000
            and e.get("retry_at", 0) <= now
        ]
        before = len(data["entries"])
        data["entries"][:] = [
            e for e in data["entries"]
            if not (e in due and e["kind"] == "mute")
            and not (
                e["status"] == "failed"
                and e.get("failed_at", now) < now - MODERATION_KEEP_FAILED_SECONDS
            )
        ]
        if len(data["entries"]) != before:
            save_moderation()
    for entry in due:
        if entry["kind"] != "ban":
            continue
        try:
            done = bool(session) and lift(entry, session["token"])
        except ApiError:
            done = False
        with moderation_lock:
            if entry not in moderation["entries"]:
                continue
            if done:
                moderation["entries"].remove(entry)
            elif now * 1000 - entry["until"] > SCHEDULE_GIVE_UP_SECONDS * 1000:
                # The owner signed out everywhere: the ban stays until the
                # owner lifts it by hand.
                entry["status"] = "failed"
                entry["failed_at"] = int(now)
            else:
                entry["retry_at"] = int(now) + 60
            save_moderation()



# Link previews for the web version. A browser may not read other websites,
# so a signed-in person can ask the server to fetch a page (or its picture)
# for them. Only public internet addresses are fetched, never this server,
# the home network or anything else private, and only pages and pictures.
FETCH_MAX_BYTES = 1024 * 1024
FETCH_TIMEOUT = 8
FETCH_REDIRECTS = 4
FETCH_TYPES = ("text/html", "application/xhtml+xml")
FETCH_AGENT = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Mobile Safari/537.36"
)


def public_address(host, port):
    """The first public internet address of [host], or an error."""
    try:
        found = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as error:
        raise ApiError(404, "DARTCHAT_FETCH_FAILED", "Unknown address.") from error
    for family, _, _, _, address in found:
        ip = ipaddress.ip_address(address[0].split("%")[0])
        if ip.is_global and not ip.is_multicast:
            return family, address
    raise ApiError(403, "DARTCHAT_NOT_PUBLIC", "Only public websites are fetched.")


class PinnedConnection(http.client.HTTPConnection):
    """Connects to an address checked beforehand, so the name cannot point
    somewhere private by the time the connection is made."""

    def __init__(self, host, port, target, secure):
        super().__init__(host, port, timeout=FETCH_TIMEOUT)
        self.target = target
        self.secure = secure

    def connect(self):
        family, address = self.target
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(address)
            if self.secure:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=self.host
                )
        except Exception:
            sock.close()
            raise
        self.sock = sock


def fetch_link(handler, body):
    caller(handler)
    url = body.get("url")
    accept = body.get("accept") or "text/html"
    try:
        limit = min(int(body.get("max_bytes") or FETCH_MAX_BYTES), FETCH_MAX_BYTES)
    except (TypeError, ValueError):
        limit = FETCH_MAX_BYTES
    if not isinstance(url, str) or len(url) > 2048 or not isinstance(accept, str):
        raise ApiError(400, "M_BAD_JSON", "Send the link to fetch.")
    for _ in range(FETCH_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ApiError(400, "M_BAD_JSON", "Only web links are fetched.")
        secure = parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        target = public_address(parts.hostname, port)
        connection = PinnedConnection(parts.hostname, port, target, secure)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            connection.request(
                "GET",
                path,
                headers={
                    "User-Agent": FETCH_AGENT,
                    "Accept": accept[:200],
                    "Accept-Language": "en",
                },
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    break
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                break
            kind = (response.getheader("Content-Type") or "").split(";")[0].strip()
            kind = kind.lower()
            is_page = kind in FETCH_TYPES
            if not is_page and not kind.startswith("image/"):
                raise ApiError(415, "DARTCHAT_FETCH_TYPE", "Not a page or a picture.")
            data = response.read(limit + 1)
            if len(data) > limit:
                if not is_page:
                    raise ApiError(413, "M_TOO_LARGE", "The picture is too large.")
                data = data[:limit]
            return {"type": kind, "data": base64.b64encode(data).decode()}
        except (OSError, http.client.HTTPException, ssl.SSLError) as error:
            raise ApiError(502, "DARTCHAT_FETCH_FAILED", "Could not fetch it.") from error
        finally:
            connection.close()
    raise ApiError(404, "DARTCHAT_FETCH_FAILED", "Could not fetch it.")

# --- Linking a computer by QR code -----------------------------------------
#
# The computer (web or desktop app) asks for a link and shows its id and a
# public key as a QR code. A phone that is signed in scans it and approves.
# The server then signs the computer in as that person (with an appservice
# login, so nobody types a password) and hands the computer what the phone
# sent: the keys to the message backup, encrypted by the phone for the
# computer's key. The server only passes that box on; it cannot open it.

LINK_FILE = os.environ.get("DARTCHAT_LINK_FILE", "/data/link_appservice.json")
LINK_APPSERVICE_ID = "dartchat-link"
LINK_SENDER = "_dartchat_link"
LINK_SECONDS = 150
LINK_RESULT_SECONDS = 120
LINK_MAX_PENDING = 50
LINK_ID = re.compile(r"[A-Za-z0-9_-]{22,64}")

links = {}
link_lock = threading.Condition()
link_appservice_lock = threading.Lock()


def _drop_old_links(now):
    for key in [
        key
        for key, entry in links.items()
        if now > entry["expires"]
        or (entry.get("result") and now > entry["result_expires"])
    ]:
        del links[key]


def start_link(handler, body):
    """A computer asks to be linked. Returns the id for its QR code and the
    secret it polls with (which is not in the QR code)."""
    key = body.get("key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", key):
        raise ApiError(400, "M_INVALID_PARAM", "A public key is needed.")
    now = time.time()
    with link_lock:
        _drop_old_links(now)
        if len(links) >= LINK_MAX_PENDING:
            raise ApiError(429, "M_LIMIT_EXCEEDED", "Too many sign-ins waiting. Try again soon.")
        link_id = secrets.token_urlsafe(24)
        poll = secrets.token_urlsafe(24)
        links[link_id] = {
            "key": key,
            "poll": poll,
            "expires": now + LINK_SECONDS,
            "result": None,
        }
    return {"id": link_id, "poll": poll, "expires_in": LINK_SECONDS}


def poll_link(handler, body, link_id):
    """The computer waits for the phone. Answers within about 20 seconds,
    with the sign-in once a phone approved."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    poll = (query.get("poll") or [""])[0]
    deadline = time.time() + 20
    with link_lock:
        while True:
            entry = links.get(link_id)
            if entry is None or not secrets.compare_digest(entry["poll"], poll):
                raise ApiError(404, "M_NOT_FOUND", "This code has expired.")
            if entry["result"] is not None:
                del links[link_id]
                return dict(entry["result"], status="done")
            left = min(deadline, entry["expires"]) - time.time()
            if left <= 0:
                if time.time() > entry["expires"]:
                    del links[link_id]
                    raise ApiError(404, "M_NOT_FOUND", "This code has expired.")
                return {"status": "waiting"}
            link_lock.wait(left)


def link_info(handler, body, link_id):
    """What the phone shows before approving: that the code is still good."""
    caller(handler)
    with link_lock:
        entry = links.get(link_id)
        if entry is None or entry["result"] is not None or time.time() > entry["expires"]:
            raise ApiError(404, "M_NOT_FOUND", "This code has expired. Show a new one.")
        return {"key": entry["key"], "expires_in": int(entry["expires"] - time.time())}


def approve_link(handler, body, link_id):
    """A signed-in phone approves the computer: the computer is signed in
    as the same person, and gets the box the phone made for it."""
    user_id, _ = caller(handler)
    box = body.get("box")
    if not isinstance(box, dict) or len(json.dumps(box)) > 12000:
        raise ApiError(400, "M_INVALID_PARAM", "The keys for the computer are missing.")
    name = body.get("device_name")
    if not isinstance(name, str) or not name.strip():
        name = "Computer"
    with link_lock:
        entry = links.get(link_id)
        if entry is None or entry["result"] is not None or time.time() > entry["expires"]:
            raise ApiError(404, "M_NOT_FOUND", "This code has expired. Show a new one.")
        entry["claimed"] = True
    session = appservice_login(user_id, name.strip()[:60])
    with link_lock:
        entry = links.get(link_id)
        if entry is None:
            raise ApiError(404, "M_NOT_FOUND", "This code has expired. Show a new one.")
        entry["result"] = {
            "user_id": session["user_id"],
            "device_id": session["device_id"],
            "access_token": session["access_token"],
            "box": box,
        }
        entry["result_expires"] = time.time() + LINK_RESULT_SECONDS
        link_lock.notify_all()
    log("A computer was linked by a phone")
    return {"device_id": session["device_id"]}


def _load_link_appservice():
    try:
        with open(LINK_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict) and data.get("as_token"):
            return data
    except (OSError, ValueError):
        pass
    return None


def _owner_admin_command(command):
    """Runs an admin command with the owner's latest sign-in."""
    global admin_room_id
    with moderation_lock:
        session = load_moderation()["owner"]
    if not session:
        raise ApiError(
            503,
            "DARTCHAT_OWNER_NEEDED",
            "The server owner has to open Dart Chat once before computers can be linked.",
        )
    token = session["token"]
    if admin_room_id is None:
        alias = quote(f"#admins:{SERVER_NAME}")
        status, found = chat("GET", f"/directory/room/{alias}", token=token)
        if status != 200 or "room_id" not in found:
            raise ApiError(502, "DARTCHAT_COMMAND_FAILED", "The admin room was not found.")
        admin_room_id = found["room_id"]
    return admin_command(token, command)


def _register_link_appservice():
    """Registers the appservice that may sign people in on a phone's word.
    Its tokens never leave this machine."""
    data = {"as_token": secrets.token_urlsafe(32), "hs_token": secrets.token_urlsafe(32)}
    server = re.escape(SERVER_NAME)
    registration = "\n".join(
        [
            f"id: {LINK_APPSERVICE_ID}",
            "url: null",
            f"as_token: {data['as_token']}",
            f"hs_token: {data['hs_token']}",
            f"sender_localpart: {LINK_SENDER}",
            "rate_limited: false",
            "namespaces:",
            "  users:",
            "    - exclusive: false",
            f"      regex: '@.*:{server}'",
            "  aliases: []",
            "  rooms: []",
        ]
    )
    # An older registration under the same id is replaced.
    try:
        _owner_admin_command(f"appservices unregister {LINK_APPSERVICE_ID}")
    except ApiError as error:
        if error.errcode == "DARTCHAT_OWNER_NEEDED":
            raise
    answer, _ = _owner_admin_command(
        f"appservices register\n```yaml\n{registration}\n```"
    )
    log(f"Registered the sign-in helper for linking computers: {answer[:120]}")
    descriptor = os.open(LINK_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return data


def _appservice_login(data, user_id, name):
    return http_json(
        "POST",
        f"{HOMESERVER}/_matrix/client/v3/login",
        {
            "type": "m.login.application_service",
            "identifier": {"type": "m.id.user", "user": user_id},
            "initial_device_display_name": name,
        },
        {"Authorization": f"Bearer {data['as_token']}"},
    )


def appservice_login(user_id, name):
    """A new sign-in (device) for [user_id], made without their password."""
    with link_appservice_lock:
        data = _load_link_appservice() or _register_link_appservice()
        try:
            status, body = _appservice_login(data, user_id, name)
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(502, "DARTCHAT_SERVER_DOWN", "The chat server is not answering.") from error
        if status in (401, 403) and body.get("errcode") in ("M_UNKNOWN_TOKEN", "M_FORBIDDEN"):
            # The registration was lost (for example a new database): again.
            data = _register_link_appservice()
            status, body = _appservice_login(data, user_id, name)
    if status != 200 or "access_token" not in body:
        log(f"Linking a computer failed: {status} {body.get('errcode')}")
        raise ApiError(502, "DARTCHAT_LINK_FAILED", "The computer could not be signed in.")
    return body



ROUTES = [
    ("GET", re.compile(r"/status"), get_status),
    ("POST", re.compile(r"/register"), register),
    ("GET", re.compile(r"/admin/invites"), list_invites),
    ("POST", re.compile(r"/admin/invites"), create_invite),
    ("DELETE", re.compile(r"/admin/invites/([0-9a-f]{16})"), revoke_invite),
    ("GET", re.compile(r"/admin/users"), list_users),
    ("POST", re.compile(r"/admin/users/deactivate"), deactivate_user),
    ("GET", re.compile(r"/admin/circles"), list_circles),
    ("POST", re.compile(r"/admin/circles"), create_circle),
    ("PUT", re.compile(r"/admin/circles/([0-9a-f]{16})"), update_circle),
    ("DELETE", re.compile(r"/admin/circles/([0-9a-f]{16})"), delete_circle),
    ("GET", re.compile(r"/scheduled"), list_scheduled),
    ("POST", re.compile(r"/scheduled"), create_scheduled),
    ("DELETE", re.compile(r"/scheduled/([0-9a-f]{16})"), cancel_scheduled),
    ("GET", re.compile(r"/moderation"), list_moderation),
    ("POST", re.compile(r"/moderation"), create_moderation),
    ("DELETE", re.compile(r"/moderation/([0-9a-f]{16})"), cancel_moderation),
    ("POST", re.compile(r"/fetch"), fetch_link),
    ("POST", re.compile(r"/link"), start_link),
    ("GET", re.compile(r"/link/([A-Za-z0-9_-]{22,64})"), poll_link),
    ("GET", re.compile(r"/link/([A-Za-z0-9_-]{22,64})/info"), link_info),
    ("POST", re.compile(r"/link/([A-Za-z0-9_-]{22,64})/approve"), approve_link),
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

    def answer_raw(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            guarded = GUARDED.fullmatch(urllib.parse.unquote(path))
            length = int(self.headers.get("Content-Length") or 0)
            if length > (
                2 * 1024 * 1024 if guarded
                else 256 * 1024 if path == PREFIX + "/scheduled"
                else 16384
            ):
                self.close_connection = True
                raise ApiError(413, "M_TOO_LARGE", "Request too large.")
            raw = self.rfile.read(length) if length else b""
            if guarded:
                self.answer_raw(*guard(self, guarded, raw))
                return
            if path.startswith("/_matrix/"):
                # Anything else the front door sends here is passed on as is.
                self.answer_raw(*forward(self, raw))
                return
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

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = handle_request


def main():
    global store
    if not SERVER_NAME or not REGISTRATION_TOKEN:
        log("FATAL: server name or registration token missing")
        return 78
    store = Store(STORE)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    threading.Thread(target=schedule_loop, daemon=True).start()
    if RESEND_KEY:
        log(f"Ready. Invitation emails are sent from {EMAIL_FROM or default_sender()}")
    else:
        log("Ready. No email key set, so invitation codes are only shown in the app")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
