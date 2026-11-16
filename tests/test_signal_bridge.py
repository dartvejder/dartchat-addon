"""The bridge between a Dart Chat group and the owner's Signal group."""

import asyncio
import base64
import struct
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from group_bot import bridge, service as service_module
from group_bot.core import Message
from group_bot.rules import RULES, RoomRules, Settings, describe

ROOM = "!grupa:t"
TOKEN = "s3cret-bridge"


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + \
        struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


class FakeClient:
    def __init__(self):
        self.rooms = {ROOM: object()}
        self.access_token = "bot-token"
        self.sent = []

    async def room_send(self, room_id, kind, content, ignore_unverified_devices=False):
        self.sent.append((room_id, content))
        return SimpleNamespace(event_id=f"$sent{len(self.sent)}")


def make_service(tmp_path, token=TOKEN, on=True):
    svc = service_module.Service.__new__(service_module.Service)
    svc.settings = Settings({"signal_bridge_token": token})
    svc.rules = RoomRules(str(tmp_path / "rooms.json"))
    if on:
        svc.rules.set(ROOM, {"signal_bridge": True})
    svc.outbox = bridge.Outbox()
    svc.bridged = {}
    svc.client = FakeClient()
    svc.bot = SimpleNamespace(ready=True)
    uploads = []

    async def upload_image(data, mime, filename):
        uploads.append((data, mime, filename))
        return {"url": "mxc://t/abc", "v": "v2"}

    svc.upload_image = upload_image
    svc.uploads = uploads
    return svc


def text_event(body):
    return SimpleNamespace(source={"content": {"msgtype": "m.text", "body": body}})


def message(body, n=1, name="Ana", media=False):
    return Message(ROOM, f"$e{n}", "@ana:t", name, body, 0, is_media=media)


def call(svc, method, path, token=TOKEN, **kw):
    async def go():
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with TestClient(TestServer(svc.bridge_app())) as client:
            resp = await getattr(client, method)(path, headers=headers, **kw)
            return resp.status, await resp.json()
    return asyncio.run(go())


def test_rule_shown_only_with_a_password():
    assert "signal_bridge" in RULES
    assert "signal_bridge" not in [r["id"] for r in describe(Settings({}))]
    assert "signal_bridge" in [r["id"] for r in describe(Settings({"signal_bridge_token": "x"}))]


def test_signal_text():
    assert bridge.signal_text("Marko", "zdravo") == "Marko: zdravo"
    assert bridge.signal_text("Marko", "") == "Marko:"
    assert bridge.signal_text("Marko", "da", {"name": "Ana", "text": "ideš?"}) == \
        "↪ Ana: ideš?\nMarko: da"


def test_outbox_cursor_and_restart():
    async def go():
        box = bridge.Outbox()
        box.add("Ana", "1")
        box.add("Ana", "2")
        assert [i["text"] for i in box.after(0)] == ["1", "2"]
        assert [i["text"] for i in box.after(1)] == ["2"]
        assert box.after(2) == []
        # The Signal bot remembers a later number than this bot knows: a restart.
        box.add("Ana", "3")
        assert [i["text"] for i in box.after(99)] == ["3"]
        waiter = asyncio.create_task(box.wait(3, 5))
        await asyncio.sleep(0.05)
        box.add("Ana", "4")
        assert [i["text"] for i in await waiter] == ["4"]
    asyncio.run(go())


def test_outbox_is_bounded():
    async def go():
        box = bridge.Outbox()
        for n in range(bridge.MAX_ITEMS + 5):
            box.add("Ana", str(n))
        assert len(box.items) == bridge.MAX_ITEMS
    asyncio.run(go())


def test_image_size():
    assert bridge.image_size(png(640, 480)) == (640, 480)
    assert bridge.image_size(b"GIF89a" + struct.pack("<HH", 30, 20)) == (30, 20)
    jpeg = b"\xff\xd8\xff\xe0" + struct.pack(">H", 4) + b"JF" + \
        b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 300, 400) + b"\x03"
    assert bridge.image_size(jpeg) == (400, 300)
    assert bridge.image_size(b"nonsense") is None


def test_dart_text_goes_to_signal_with_reply(tmp_path):
    svc = make_service(tmp_path)

    async def go():
        await svc.to_signal(None, text_event("ideš?"), message("ideš?", 1), None)
        await svc.to_signal(None, text_event("> <@ana:t> ideš?\n\nda"),
                            message("> <@ana:t> ideš?\n\nda", 2, "Marko"), "$e1")
    asyncio.run(go())
    items = svc.outbox.after(0)
    assert [(i["name"], i["text"]) for i in items] == [("Ana", "ideš?"), ("Marko", "da")]
    assert items[1]["quote"] == {"name": "Ana", "text": "ideš?"}


def test_dart_photo_goes_to_signal(tmp_path):
    svc = make_service(tmp_path)

    async def download_image(event):
        return png(2, 2)

    svc.download_image = download_image
    event = SimpleNamespace(source={"content": {"msgtype": "m.image", "body": "IMG_1.jpg",
                                                "info": {"mimetype": "image/png"}}})
    asyncio.run(svc.to_signal(None, event, message("IMG_1.jpg", 1, media=True), None))
    item = svc.outbox.after(0)[0]
    assert item["text"] == ""
    assert item["image"]["mime"] == "image/png"
    assert base64.b64decode(item["image"]["data"]) == png(2, 2)


def test_switched_off_group_is_not_bridged(tmp_path):
    svc = make_service(tmp_path, on=False)
    assert not svc.bridge_on(ROOM)
    assert asyncio.run(svc.from_signal({"name": "Marko", "text": "hej"})) == 0
    assert svc.client.sent == []
    # No password set: the rule does nothing even if it was left on.
    svc = make_service(tmp_path, token="")
    assert not svc.bridge_on(ROOM)


def test_signal_message_with_photo_reaches_the_group(tmp_path):
    svc = make_service(tmp_path)
    picture = base64.b64encode(png(800, 600)).decode()
    status, answer = call(svc, "post", "/_dartchat/v1/bridge/inbox", json={
        "name": "Marko", "text": "vidi ovo",
        "images": [{"data": picture, "mime": "image/png", "filename": "a.png"}]})
    assert status == 200 and answer == {"delivered": 1, "on": True}
    (r1, text), (r2, image) = svc.client.sent
    assert text["msgtype"] == "m.text" and text["body"] == "Marko: vidi ovo"
    assert image["msgtype"] == "m.image" and image["file"]["url"] == "mxc://t/abc"
    assert image["info"]["w"] == 800 and image["info"]["h"] == 600
    assert svc.uploads[0][1:] == ("image/png", "a.png")


def test_endpoints_need_the_password(tmp_path):
    svc = make_service(tmp_path)
    assert call(svc, "get", "/_dartchat/v1/bridge/status", token=None)[0] == 401
    assert call(svc, "get", "/_dartchat/v1/bridge/status", token="wrong")[0] == 401
    assert call(svc, "get", "/_dartchat/v1/bridge/status") == (200, {"on": True, "ready": True})


def test_outbox_endpoint(tmp_path):
    svc = make_service(tmp_path)

    async def go():
        svc.outbox.add("Ana", "zdravo")
    asyncio.run(go())
    status, answer = call(svc, "get", "/_dartchat/v1/bridge/outbox?after=0&wait=0")
    assert status == 200 and [i["text"] for i in answer["items"]] == ["zdravo"]
    assert answer["last"] == 1
    status, answer = call(svc, "get", "/_dartchat/v1/bridge/outbox?after=1&wait=0")
    assert answer["items"] == []
