#!/usr/bin/env python3
"""Dart Chat push gateway.

The chat server calls this small service on the same machine whenever a
signed-in phone has something new. The gateway asks Firebase to wake that
phone. The wake-up carries only an event ID, a room ID and an unread count:
never the message, the sender or the chat name. The one other wake-up, sent
when an invited friend creates their account, says only that someone joined. iPhones get a fixed
"New message" notice instead of a silent wake-up.

It uses nothing outside the Python standard library, plus the `openssl`
command to sign the request for a Firebase access token.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = ("127.0.0.1", int(os.environ.get("DARTCHAT_PUSH_PORT", "5000")))
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
# Overridable so the gateway can be tested without contacting Google.
FCM_BASE = os.environ.get("DARTCHAT_FCM_BASE", "https://fcm.googleapis.com")
NOTIFY_PATH = "/_matrix/push/v1/notify"
MAX_BODY = 64 * 1024


def log(text):
    print(f"[dartchat-push] {text}", flush=True)


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Firebase:
    """Sends wake-ups through Firebase Cloud Messaging."""

    def __init__(self, account):
        self.project = account["project_id"]
        self.email = account["client_email"]
        self.token_uri = account.get("token_uri", "https://oauth2.googleapis.com/token")
        # The key stays in a file only this process can read, in memory-backed
        # storage where available, so openssl can use it.
        key_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None
        handle, self.key_path = tempfile.mkstemp(dir=key_dir)
        with os.fdopen(handle, "w") as key_file:
            key_file.write(account["private_key"])
        self._lock = threading.Lock()
        self._token = None
        self._expires = 0.0

    def _sign(self, text):
        return subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", self.key_path],
            input=text.encode(),
            capture_output=True,
            check=True,
        ).stdout

    def _access_token(self):
        with self._lock:
            now = time.time()
            if self._token and now < self._expires - 60:
                return self._token
            header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            claims = b64url(
                json.dumps(
                    {
                        "iss": self.email,
                        "scope": SCOPE,
                        "aud": self.token_uri,
                        "iat": int(now),
                        "exp": int(now) + 3600,
                    }
                ).encode()
            )
            unsigned = f"{header}.{claims}"
            assertion = f"{unsigned}.{b64url(self._sign(unsigned))}"
            body = urllib.parse.urlencode(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                }
            ).encode()
            with urllib.request.urlopen(self.token_uri, body, timeout=15) as reply:
                data = json.load(reply)
            self._token = data["access_token"]
            self._expires = now + int(data.get("expires_in", 3600))
            return self._token

    def wake(self, push_key, data, app_id=""):
        """Returns True when the phone no longer exists and should be dropped."""
        message = fcm_message(push_key, data, app_id)
        request = urllib.request.Request(
            f"{FCM_BASE}/v1/projects/{self.project}/messages:send",
            data=json.dumps(message).encode(),
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                return False
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            if error.code == 404 or "UNREGISTERED" in detail:
                return True
            if error.code == 401:
                with self._lock:
                    self._token = None
            raise RuntimeError(f"Firebase answered {error.code}") from error


def is_iphone(app_id):
    return str(app_id or "").endswith(".ios")


def fcm_message(push_key, data, app_id=""):
    """Builds what Firebase is asked to deliver to one phone.

    An Android phone gets a silent wake-up and shows "New message" itself.
    An iPhone cannot be woken that way reliably, so Apple shows a fixed
    "New message" notice for it. It still names no message, sender or chat.
    """
    message = {"token": push_key, "data": data}
    if not is_iphone(app_id):
        message["android"] = {"priority": "high"}
        return {"message": message}
    try:
        unread = max(0, int(data.get("unread", "0")))
    except ValueError:
        unread = 0
    if unread == 0:
        # Everything was read elsewhere: clear the app icon's counter quietly.
        aps = {"badge": 0, "content-available": 1}
        headers = {"apns-push-type": "background", "apns-priority": "5"}
    else:
        joined = data.get("kind") == "joined"
        aps = {
            "alert": {
                "title": "Dart Chat",
                "body": "Someone you invited has joined" if joined else "New message",
            },
            "sound": "default",
            "badge": unread,
        }
        # One notice at a time, like on Android: a newer one replaces it.
        headers = {
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-collapse-id": "dartchat-joined" if joined else "dartchat-new-message",
        }
    message["apns"] = {"headers": headers, "payload": {"aps": aps}}
    return {"message": message}


def wake_data(notification):
    """The only things that ever leave the server. All values are strings."""
    data = {}
    for key in ("event_id", "room_id"):
        if notification.get(key):
            data[key] = str(notification[key])
    counts = notification.get("counts") or {}
    data["unread"] = str(counts.get("unread", 0))
    # Sent by the invitation service when an invited friend creates their
    # account. It says only that someone joined, not who.
    if notification.get("kind") == "joined":
        data["kind"] = "joined"
    return data


def make_handler(firebase):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != NOTIFY_PATH:
                return self._reply(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY:
                    return self._reply(400, {"error": "bad length"})
                notification = json.loads(self.rfile.read(length))["notification"]
                devices = notification.get("devices") or []
            except (ValueError, KeyError, TypeError, AttributeError):
                return self._reply(400, {"error": "bad request"})

            data = wake_data(notification)
            rejected = []
            failed = False
            for device in devices:
                push_key = device.get("pushkey") if isinstance(device, dict) else None
                if not push_key:
                    continue
                try:
                    if firebase.wake(push_key, data, device.get("app_id", "")):
                        rejected.append(push_key)
                except Exception as error:  # keep serving whatever happens
                    failed = True
                    log(f"Could not send a wake-up: {error}")
            if failed and not rejected:
                # Tells the chat server to try again later.
                return self._reply(502, {"error": "upstream failure"})
            self._reply(200, {"rejected": rejected})

        def log_message(self, *args):
            pass

    return Handler


def main():
    raw = os.environ.get("DARTCHAT_FIREBASE_ACCOUNT", "").strip()
    try:
        account = json.loads(raw)
        for field in ("project_id", "client_email", "private_key"):
            if not account.get(field):
                raise ValueError(f"'{field}' is missing")
    except (ValueError, AttributeError) as error:
        log(f"The Firebase service account is not valid ({error}). Notifications are off.")
        # Exit code 78 tells the start script not to restart us.
        sys.exit(78)
    firebase = Firebase(account)
    server = ThreadingHTTPServer(LISTEN, make_handler(firebase))
    log(f"Ready. Sending wake-ups through Firebase project {firebase.project}")
    server.serve_forever()


if __name__ == "__main__":
    main()
