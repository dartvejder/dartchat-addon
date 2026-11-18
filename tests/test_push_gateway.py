"""Unit tests for what the push gateway asks Firebase to deliver."""

from push_gateway import fcm_message, wake_data

DATA = {"event_id": "$e", "room_id": "!r", "unread": "3"}


def test_android_gets_a_silent_high_priority_wake_up():
    message = fcm_message("tok", DATA, "com.dartvejder.dartchat.android")["message"]
    assert message == {"token": "tok", "data": DATA, "android": {"priority": "high"}}


def test_unknown_app_is_treated_like_android():
    assert "android" in fcm_message("tok", DATA)["message"]


def test_iphone_gets_a_fixed_notice_with_the_unread_count():
    message = fcm_message("tok", DATA, "com.dartvejder.dartchat.ios")["message"]
    assert "android" not in message
    aps = message["apns"]["payload"]["aps"]
    assert aps["alert"] == {"title": "Dart Chat", "body": "New message"}
    assert aps["badge"] == 3
    assert message["apns"]["headers"]["apns-push-type"] == "alert"


def test_iphone_message_notice_can_be_named_on_the_phone():
    message = fcm_message("tok", DATA, "com.dartvejder.dartchat.ios")["message"]
    assert message["apns"]["payload"]["aps"]["mutable-content"] == 1


def test_iphone_all_read_clears_the_counter_quietly():
    data = dict(DATA, unread="0")
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")["message"]
    aps = message["apns"]["payload"]["aps"]
    assert aps == {"badge": 0, "content-available": 1}
    assert message["apns"]["headers"]["apns-push-type"] == "background"


def test_nothing_private_leaves_the_server():
    data = wake_data(
        {
            "event_id": "$e",
            "room_id": "!r",
            "sender": "@a:x",
            "room_name": "Secret",
            "content": {"body": "hi"},
            "counts": {"unread": 2},
        }
    )
    assert data == {"event_id": "$e", "room_id": "!r", "unread": "2"}
    message = fcm_message("tok", data, "com.dartvejder.dartchat.ios")
    text = str(message)
    for private in ("@a:x", "Secret", "hi"):
        assert private not in text


# --- Web Push (the web version) ---------------------------------------------

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import push_gateway
from push_gateway import WebPush, make_handler, parse_web_push_key, web_push_payload


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Browser:
    """Plays the browser's side: its keys, and decrypting what arrives."""

    def __init__(self, endpoint):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.auth = b"0123456789abcdef"
        self.push_key = f"{endpoint} {_b64(self.public)} {_b64(self.auth)}"

    def decrypt(self, body):
        salt, rs, idlen = body[:16], body[16:20], body[20]
        sender_public = body[21 : 21 + idlen]
        cipher = body[21 + idlen :]
        sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public)
        shared = self.key.exchange(ec.ECDH(), sender)

        def hkdf(s, ikm, info, n):
            return HKDF(hashes.SHA256(), n, s, info).derive(ikm)

        ikm = hkdf(self.auth, shared, b"WebPush: info\x00" + self.public + sender_public, 32)
        key = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
        nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
        plain = AESGCM(key).decrypt(nonce, cipher, None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


@pytest.fixture
def web_push(tmp_path):
    return WebPush(key_file=str(tmp_path / "vapid.pem"), subject="https://chat.test")


def test_the_web_push_key_is_made_once_and_kept(tmp_path):
    first = WebPush(key_file=str(tmp_path / "vapid.pem"))
    second = WebPush(key_file=str(tmp_path / "vapid.pem"))
    assert first.public_key == second.public_key
    assert len(_unb64(first.public_key)) == 65


def test_browser_registration_is_parsed():
    browser = Browser("https://push.example/abc")
    endpoint, public, secret = parse_web_push_key(browser.push_key)
    assert endpoint == "https://push.example/abc"
    assert public == browser.public and secret == browser.auth
    for bad in ("", "fcm-token", "http://x a b", "https://x a"):
        with pytest.raises(ValueError):
            parse_web_push_key(bad)


def test_browser_payload_holds_nothing_private():
    data = push_gateway.wake_data(
        {"event_id": "$e", "room_id": "!r", "sender": "@a:x",
         "room_name": "Secret", "content": {"body": "hi"}, "counts": {"unread": 2}}
    )
    payload = web_push_payload(data)
    assert json.loads(payload) == {"unread": "2", "room_id": "!r"}
    for private in (b"@a:x", b"Secret", b"hi"):
        assert private not in payload


class _PushService:
    """A browser push service on this machine that records what it gets."""

    def __init__(self, status=201):
        self.received = []
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                service.received.append(
                    (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
                )
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def test_browser_gets_an_encrypted_signed_message(web_push, monkeypatch):
    service = _PushService()
    browser = Browser("https://push.example/sub1")
    # Send to the local service instead of the real address.
    monkeypatch.setattr(
        push_gateway, "parse_web_push_key",
        lambda key: (service.url + "/sub1",) + parse_web_push_key(key)[1:],
    )
    try:
        assert web_push.wake(browser.push_key, {"room_id": "!r", "unread": "1"}) is False
    finally:
        service.close()
    path, headers, body = service.received[0]
    assert path == "/sub1"
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["ttl"] == "86400"
    assert browser.decrypt(body) == {"unread": "1", "room_id": "!r"}
    # The signature checks out against the key the web app is given.
    auth = headers["authorization"]
    token = auth.split("t=")[1].split(",")[0]
    assert auth.endswith(f"k={web_push.public_key}")
    head, claims, signature = token.split(".")
    assert json.loads(_unb64(claims))["aud"] == service.url
    assert json.loads(_unb64(claims))["sub"] == "https://chat.test"
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), _unb64(web_push.public_key)
    )
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    raw = _unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_all_read_is_not_sent_to_browsers(web_push):
    browser = Browser("https://push.invalid/sub")
    # Would fail to connect if it tried to send.
    assert web_push.wake(browser.push_key, {"room_id": "!r", "unread": "0"}) is False


def test_a_browser_that_is_gone_is_dropped(web_push, monkeypatch):
    service = _PushService(status=410)
    browser = Browser("https://push.example/old")
    monkeypatch.setattr(
        push_gateway, "parse_web_push_key",
        lambda key: (service.url + "/old",) + parse_web_push_key(key)[1:],
    )
    try:
        assert web_push.wake(browser.push_key, {"room_id": "!r", "unread": "3"}) is True
    finally:
        service.close()


def test_gateway_routes_browsers_to_web_push_and_serves_the_key(web_push):
    class FakeFirebase:
        def __init__(self):
            self.woken = []

        def wake(self, push_key, data, app_id=""):
            self.woken.append(push_key)
            return False

    class FakeWebPush:
        public_key = web_push.public_key

        def __init__(self):
            self.woken = []

        def wake(self, push_key, data):
            self.woken.append(push_key)
            return True

    firebase, browser = FakeFirebase(), FakeWebPush()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(firebase, browser))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    import urllib.request

    try:
        body = json.dumps({"notification": {"room_id": "!r", "counts": {"unread": 1},
            "devices": [
                {"app_id": "com.dartvejder.dartchat.android", "pushkey": "phone"},
                {"app_id": "com.dartvejder.dartchat.web", "pushkey": "browser"},
            ]}}).encode()
        with urllib.request.urlopen(base + "/_matrix/push/v1/notify", body) as reply:
            assert json.load(reply) == {"rejected": ["browser"]}
        with urllib.request.urlopen(base + "/_dartchat/v1/webpush/key") as reply:
            assert json.load(reply) == {"key": web_push.public_key}
    finally:
        server.shutdown()
    assert firebase.woken == ["phone"] and browser.woken == ["browser"]
