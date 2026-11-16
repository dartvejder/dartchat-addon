#!/usr/bin/env python3
"""Dart Chat push gateway.

The chat server calls this small service on the same machine whenever a
signed-in phone has something new. The gateway asks Firebase to wake that
phone. The wake-up carries only an event ID, a room ID and an unread count:
never the message, the sender or the chat name. The one other wake-up, sent
when an invited friend creates their account, says only that someone joined. iPhones get a fixed
"New message" notice instead of a silent wake-up.

Browsers (the web version, also when installed as an app on a computer) are
woken through the browser's own push service with Web Push. What they get is
encrypted for that browser and says only "something new", the chat it is in
and the unread count; the browser shows a fixed "New message" notice.

It uses the Python standard library, the `openssl` command to sign the
request for a Firebase access token and, for Web Push, the `cryptography`
package.
"""

import base64
import json
import os
import struct
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
# The web app asks for the key browsers check Web Push messages against.
WEB_KEY_PATH = "/_dartchat/v1/webpush/key"
MAX_BODY = 64 * 1024
# The app id of the web version (also when installed on a computer).
WEB_APP_ID = "com.dartvejder.dartchat.web"
VAPID_KEY_FILE = os.environ.get("DARTCHAT_VAPID_KEY_FILE", "/data/webpush_vapid.pem")


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


def b64url_decode(text):
    text = str(text)
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_web(app_id):
    return str(app_id or "") == WEB_APP_ID


def parse_web_push_key(push_key):
    """A browser's registration: "<endpoint> <p256dh> <auth>".

    The chat server keeps one push key per device, so the browser's address
    and its two encryption keys travel together in it.
    """
    parts = str(push_key or "").split(" ")
    if len(parts) != 3 or not parts[0].startswith("https://"):
        raise ValueError("not a browser registration")
    endpoint, p256dh, auth = parts
    public = b64url_decode(p256dh)
    secret = b64url_decode(auth)
    if len(public) != 65 or public[0] != 4 or len(secret) != 16:
        raise ValueError("bad browser keys")
    return endpoint, public, secret


def web_push_payload(data):
    """What the browser receives, before encryption. Never the message, the
    sender or the chat name: only that something is new, which chat, and how
    many are unread, plus the owner's notices."""
    payload = {"unread": data.get("unread", "0")}
    if data.get("room_id"):
        payload["room_id"] = data["room_id"]
    if data.get("kind") in NOTICES:
        payload["kind"] = data["kind"]
    return json.dumps(payload, separators=(",", ":")).encode()


class WebPush:
    """Sends encrypted Web Push messages (RFC 8291, RFC 8292)."""

    def __init__(self, key_file=VAPID_KEY_FILE, subject="https://localhost"):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self._ec = ec
        self._serialization = serialization
        self.subject = subject
        try:
            with open(key_file, "rb") as handle:
                self._key = serialization.load_pem_private_key(handle.read(), None)
        except FileNotFoundError:
            # Made once and kept: a new key would cut off every browser
            # that registered with the old one.
            self._key = ec.generate_private_key(ec.SECP256R1())
            pem = self._key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            directory = os.path.dirname(key_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            handle = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "wb") as out:
                out.write(pem)
        self.public_key = b64url(self._public_bytes(self._key.public_key()))
        self._lock = threading.Lock()
        self._tokens = {}

    def _public_bytes(self, key):
        return key.public_bytes(
            self._serialization.Encoding.X962,
            self._serialization.PublicFormat.UncompressedPoint,
        )

    def _vapid(self, endpoint):
        """The signed note that tells the browser's push service who sends."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        url = urllib.parse.urlsplit(endpoint)
        audience = f"{url.scheme}://{url.netloc}"
        now = time.time()
        with self._lock:
            cached = self._tokens.get(audience)
            if cached and now < cached[1] - 600:
                return cached[0]
            expires = int(now) + 12 * 3600
            header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
            claims = b64url(
                json.dumps({"aud": audience, "exp": expires, "sub": self.subject}).encode()
            )
            unsigned = f"{header}.{claims}"
            der = self._key.sign(unsigned.encode(), self._ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
            token = f"{unsigned}.{b64url(signature)}"
            self._tokens[audience] = (token, expires)
            return token

    def encrypt(self, plaintext, browser_public, auth_secret, salt=None, sender_key=None):
        """aes128gcm content encoding for one browser (RFC 8291)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        def hkdf(salt_bytes, ikm, info, length):
            return HKDF(hashes.SHA256(), length, salt_bytes, info).derive(ikm)

        ec = self._ec
        sender = sender_key or ec.generate_private_key(ec.SECP256R1())
        sender_public = self._public_bytes(sender.public_key())
        browser = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), browser_public)
        shared = sender.exchange(ec.ECDH(), browser)
        ikm = hkdf(
            auth_secret, shared, b"WebPush: info\x00" + browser_public + sender_public, 32
        )
        salt = salt or os.urandom(16)
        key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        # One record: the text, then the "last record" marker.
        body = AESGCM(key).encrypt(nonce, plaintext + b"\x02", None)
        header = salt + struct.pack(">IB", 4096, len(sender_public)) + sender_public
        return header + body

    def wake(self, push_key, data):
        """Returns True when the browser no longer exists and should be dropped."""
        try:
            endpoint, public, secret = parse_web_push_key(push_key)
        except ValueError:
            return True
        if data.get("unread") == "0" and data.get("kind") not in NOTICES:
            # A browser must show something for every message it gets, so
            # "all read" is not sent; the app clears its notices itself.
            return False
        body = self.encrypt(web_push_payload(data), public, secret)
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"vapid t={self._vapid(endpoint)}, k={self.public_key}",
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": "86400",
                "Urgency": "high",
                # A newer one replaces a waiting one, like on the phones.
                "Topic": "dartchat",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                return False
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return True
            raise RuntimeError(f"The browser's push service answered {error.code}") from error


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
        notice = NOTICES.get(data.get("kind"))
        aps = {
            "alert": {
                "title": "Dart Chat",
                "body": notice[0] if notice else "New message",
            },
            "sound": "default",
            "badge": unread,
        }
        # One notice at a time, like on Android: a newer one replaces it.
        headers = {
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-collapse-id": notice[1] if notice else "dartchat-new-message",
        }
    message["apns"] = {"headers": headers, "payload": {"aps": aps}}
    return {"message": message}


# The owner's notices: the fixed iPhone text and what replaces what.
NOTICES = {
    "joined": ("Someone you invited has joined", "dartchat-joined"),
    "renamed": ("Someone changed their name", "dartchat-renamed"),
}


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
    # "renamed" says, just as bare, that someone changed their display name.
    if notification.get("kind") in NOTICES:
        data["kind"] = notification["kind"]
    return data


def make_handler(firebase, web_push=None):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path != WEB_KEY_PATH or web_push is None:
                return self._reply(404, {"error": "not found"})
            self._reply(200, {"key": web_push.public_key})

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
                app_id = device.get("app_id", "")
                sender = web_push if is_web(app_id) else firebase
                if sender is None:
                    continue
                try:
                    if is_web(app_id):
                        gone = web_push.wake(push_key, data)
                    else:
                        gone = firebase.wake(push_key, data, app_id)
                    if gone:
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
    firebase = None
    raw = os.environ.get("DARTCHAT_FIREBASE_ACCOUNT", "").strip()
    if raw:
        try:
            account = json.loads(raw)
            for field in ("project_id", "client_email", "private_key"):
                if not account.get(field):
                    raise ValueError(f"'{field}' is missing")
            firebase = Firebase(account)
        except (ValueError, AttributeError) as error:
            log(f"The Firebase service account is not valid ({error}). Phone notifications are off.")
    web_push = None
    try:
        server_name = os.environ.get("DARTCHAT_SERVER_NAME", "") or "localhost"
        web_push = WebPush(subject=f"https://{server_name}")
    except Exception as error:  # the phones still get theirs
        log(f"Web Push is unavailable ({error}). Browser notifications are off.")
    if firebase is None and web_push is None:
        # Exit code 78 tells the start script not to restart us.
        sys.exit(78)
    server = ThreadingHTTPServer(LISTEN, make_handler(firebase, web_push))
    if firebase:
        log(f"Ready. Sending wake-ups through Firebase project {firebase.project}")
    if web_push:
        log("Ready. Sending browser wake-ups through Web Push")
    server.serve_forever()


if __name__ == "__main__":
    main()
