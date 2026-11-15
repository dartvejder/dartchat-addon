"""Tests for the link fetch that gives the web version its link previews."""

import base64
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import invite_service as service


class Site(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/page":
            body, kind = b"<html><title>Hello</title></html>", "text/html; charset=utf-8"
        elif self.path == "/big.png":
            body, kind = b"x" * 5000, "image/png"
        elif self.path == "/file.zip":
            body, kind = b"PK", "application/zip"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    monkeypatch.setattr(service, "caller", lambda handler: ("@ana:chat.test", "t"))
    # Pretend the test site is on the public internet.
    monkeypatch.setattr(
        service,
        "public_address",
        lambda host, p: (socket.AF_INET, ("127.0.0.1", port)),
    )
    yield f"http://site.test:{port}"
    server.shutdown()


def test_private_addresses_are_refused():
    for host in ("127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10", "169.254.169.254", "::1"):
        with pytest.raises(service.ApiError) as error:
            service.public_address(host, 80)
        assert error.value.status == 403


def test_needs_sign_in():
    class Request:
        headers = {}
        path = "/_dartchat/v1/fetch"

    with pytest.raises(service.ApiError) as error:
        service.fetch_link(Request(), {"url": "https://example.com"})
    assert error.value.status == 401


def test_fetches_page_after_redirect(site):
    answer = service.fetch_link(None, {"url": site + "/moved"})
    assert answer["type"] == "text/html"
    assert b"<title>Hello</title>" in base64.b64decode(answer["data"])


def test_limits(site):
    page = service.fetch_link(None, {"url": site + "/page", "max_bytes": 10})
    assert len(base64.b64decode(page["data"])) == 10  # a page is cut short
    with pytest.raises(service.ApiError) as error:
        service.fetch_link(None, {"url": site + "/big.png", "max_bytes": 100})
    assert error.value.status == 413  # a picture is not
    with pytest.raises(service.ApiError) as error:
        service.fetch_link(None, {"url": site + "/file.zip"})
    assert error.value.status == 415
    with pytest.raises(service.ApiError):
        service.fetch_link(None, {"url": "file:///etc/passwd"})
