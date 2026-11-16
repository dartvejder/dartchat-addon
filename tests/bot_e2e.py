#!/usr/bin/env python3
"""End-to-end test of the group-rules bot with real encrypted chats.

Usage: bot_e2e.py <server url> <owner user> <owner password>
                  <friend user> <friend password> [bot api url]

The owner and the friend sign in with encryption on (like the app), the
owner makes an encrypted group, switches rules on for it, and the test checks
that the bot replies to a repeated link and answers a command, and leaves
when the rules are switched off. With BOT_RESTART_CMD set, it also restarts the
bot in the middle. Needs matrix-nio[e2e].
"""

import asyncio
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

import aiohttp
from nio import (AsyncClient, AsyncClientConfig, RoomEncryptedImage, RoomMessageNotice,
                 RoomMessageText)
from nio.crypto import attachments

BASE = sys.argv[1]
OWNER, OWNER_PASSWORD, FRIEND, FRIEND_PASSWORD = sys.argv[2:6]
BOT_API = (sys.argv[6] if len(sys.argv) > 6 else BASE) + "/_dartchat/v1/bot"
# A shell command that restarts the bot, to test that it survives one.
RESTART = os.environ.get("BOT_RESTART_CMD", "")
# The Signal bridge's password and address, when the server has one set.
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
BRIDGE = os.environ.get("BRIDGE_URL", "http://127.0.0.1:6170") + "/_dartchat/v1/bridge"
# A 2x1 PNG picture.
PICTURE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjdAAAAEElEQVR4nGP4z8DAwMDAAAAN"
    "/wH/ZcGc3gAAAABJRU5ErkJggg==")
ALL_RULES = ["repost", "same_news", "keyword", "commands", "daily_report", "ha_sensors"]


async def sign_in(user, password, folder):
    config = AsyncClientConfig(encryption_enabled=True, store_sync_tokens=False)
    client = AsyncClient(BASE, user, store_path=folder, config=config)
    response = await client.login(password, device_name="test")
    assert hasattr(response, "access_token"), response
    client.notices = []

    async def on_notice(room, event):
        client.notices.append((room.room_id, event))

    client.add_event_callback(on_notice, RoomMessageNotice)
    client.messages = []

    async def on_message(room, event):
        client.messages.append((room.room_id, event))

    client.add_event_callback(on_message, (RoomMessageText, RoomEncryptedImage))
    return client


async def step(clients, seconds=1.5):
    for client in clients:
        await client.sync(timeout=int(seconds * 1000), full_state=not client.next_batch)
        if client.should_upload_keys:
            await client.keys_upload()
        if client.should_query_keys:
            await client.keys_query()
        await client.send_to_device_messages()


async def until(clients, check, what, limit=90):
    deadline = time.time() + limit
    while time.time() < deadline:
        await step(clients)
        if check():
            return
    raise AssertionError(f"timed out waiting for {what}: " + repr(
        [(c.user_id, list(c.rooms), list(c.invited_rooms)) for c in clients]))


async def api(http, method, path, token, body=None):
    headers = {"Authorization": f"Bearer {token}"}
    async with http.request(method, BOT_API + path, json=body, headers=headers) as resp:
        return resp.status, await resp.json(content_type=None)


async def bot_ready(http, owner):
    deadline = time.time() + 120
    while True:
        try:
            status, body = await api(http, "GET", "/status", owner.access_token)
        except (aiohttp.ClientError, ValueError):
            status, body = 0, {}
        if status == 200 and body["ready"]:
            return body
        assert status in (0, 200, 502), (status, body)
        assert time.time() < deadline, "the bot did not get ready"
        await asyncio.sleep(2)


async def send(client, room_id, text):
    response = await client.room_send(room_id, "m.room.message",
                                      {"msgtype": "m.text", "body": text},
                                      ignore_unverified_devices=True)
    assert getattr(response, "event_id", None), response
    return response.event_id


async def bridge_api(http, method, path, token=None, body=None):
    headers = {"Authorization": f"Bearer {token or BRIDGE_TOKEN}"}
    async with http.request(method, BRIDGE + path, json=body, headers=headers) as resp:
        return resp.status, await resp.json(content_type=None)


async def check_bridge(http, clients, owner, friend, room_id, path):
    """Messages and photos pass both ways while the rule is on."""
    status, body = await bridge_api(http, "GET", "/status", token="wrong")
    assert status == 401, (status, body)
    status, body = await api(http, "GET", "/status", owner.access_token)
    assert "signal_bridge" in [r["id"] for r in body["rules"]], body
    status, body = await api(http, "PUT", path, owner.access_token,
                             {"rules": {"commands": True, "signal_bridge": True}})
    assert status == 200 and body["rules"]["signal_bridge"], (status, body)
    status, body = await bridge_api(http, "GET", "/outbox?wait=0")
    cursor = body["last"]

    # Dart Chat to Signal: a text and an encrypted photo.
    await send(friend, room_id, "most radi")
    upload, keys = await friend.upload(io.BytesIO(PICTURE), content_type="image/png",
                                       filename="slika.png", encrypt=True,
                                       filesize=len(PICTURE))
    assert getattr(upload, "content_uri", None), upload
    await friend.room_send(room_id, "m.room.message", {
        "msgtype": "m.image", "body": "slika.png",
        "info": {"mimetype": "image/png", "size": len(PICTURE), "w": 2, "h": 1},
        "file": {"url": upload.content_uri, **keys}}, ignore_unverified_devices=True)
    items = []
    deadline = time.time() + 60
    while len(items) < 2 and time.time() < deadline:
        await step(clients, 0.5)
        status, body = await bridge_api(http, "GET", f"/outbox?after={cursor}&wait=2")
        assert status == 200, (status, body)
        items = body["items"]
    assert [i["text"] for i in items] == ["most radi", ""], items
    assert base64.b64decode(items[1]["image"]["data"]) == PICTURE, items[1]
    assert items[0]["name"] == items[1]["name"]

    # Signal to Dart Chat: the owner's app reads it like any message.
    status, body = await bridge_api(http, "POST", "/inbox", body={
        "name": "Signal Marko", "text": "stiže", "signal_ts": 777,
        "images": [{"data": base64.b64encode(PICTURE).decode(), "mime": "image/png",
                    "filename": "s.png"}]})
    assert status == 200 and body["delivered"] == 1, (status, body)

    def arrived():
        texts = [e for r, e in owner.messages if r == room_id
                 and isinstance(e, RoomMessageText) and e.body == "Signal Marko: stiže"]
        images = [e for r, e in owner.messages if r == room_id
                  and isinstance(e, RoomEncryptedImage)]
        return texts and images and images[-1]
    await until(clients, arrived, "the message from Signal")
    image = arrived()
    server, media = image.url[len("mxc://"):].split("/", 1)
    async with http.get(f"{BASE}/_matrix/client/v1/media/download/{server}/{media}",
                        headers={"Authorization": f"Bearer {owner.access_token}"}) as r:
        assert r.status == 200, await r.text()
        data = attachments.decrypt_attachment(await r.read(), image.key["k"],
                                              image.hashes["sha256"], image.iv)
    assert data == PICTURE

    # Edits pass both ways.
    status, body = await bridge_api(http, "GET", "/outbox?wait=0")
    cursor = body["last"]
    text_id = items[0]["event_id"]
    await friend.room_send(room_id, "m.room.message", {
        "msgtype": "m.text", "body": "* most radi!",
        "m.new_content": {"msgtype": "m.text", "body": "most radi!"},
        "m.relates_to": {"rel_type": "m.replace", "event_id": text_id}},
        ignore_unverified_devices=True)
    edits = []
    deadline = time.time() + 60
    while not edits and time.time() < deadline:
        await step(clients, 0.5)
        status, body = await bridge_api(http, "GET", f"/outbox?after={cursor}&wait=2")
        edits = body["items"]
    assert [(i["text"], i.get("edit_of")) for i in edits] == [("most radi!", text_id)], edits
    status, body = await bridge_api(http, "POST", "/inbox", body={
        "name": "Signal Marko", "text": "stiže!", "edit_of": 777})
    assert status == 200 and body["delivered"] == 1, (status, body)

    def edited():
        return [e for r, e in owner.messages if r == room_id and isinstance(e, RoomMessageText)
                and (e.source["content"].get("m.new_content") or {}).get("body")
                == "Signal Marko: stiže!"]
    await until(clients, edited, "the edit from Signal")

    # Switched off: nothing passes either way.
    status, body = await api(http, "PUT", path, owner.access_token,
                             {"rules": {"commands": True}})
    assert status == 200 and not body["rules"]["signal_bridge"], (status, body)
    status, body = await bridge_api(http, "POST", "/inbox", body={"name": "X", "text": "y"})
    assert (status, body["delivered"], body["on"]) == (200, 0, False), (status, body)
    await send(friend, room_id, "ne sme da prođe")
    await step(clients)
    status, body = await bridge_api(http, "GET", "/outbox?wait=0")
    assert body["items"] == [], body
    print("Signal bridge test passed")


async def main():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        owner = await sign_in(OWNER, OWNER_PASSWORD, a)
        friend = await sign_in(FRIEND, FRIEND_PASSWORD, b)
        clients = [owner, friend]
        await step(clients)
        async with aiohttp.ClientSession() as http:
            body = await bot_ready(http, owner)
            status, _ = await api(http, "GET", "/status", friend.access_token)
            assert status == 403, status
            bot_id = body["user_id"]
            assert [r["id"] for r in body["rules"]][:2] == ["repost", "same_news"], body

            # Only the owner enters birthdays, for everyone.
            status, body = await api(http, "GET", "/birthdays", None)
            assert status == 401, (status, body)
            status, body = await api(http, "GET", "/birthdays", friend.access_token)
            assert status == 403, (status, body)
            status, body = await api(http, "PUT", "/birthday", friend.access_token,
                                     {"user_id": friend.user_id, "month": 5, "day": 17})
            assert status == 403, (status, body)
            status, body = await api(http, "PUT", "/birthday", owner.access_token,
                                     {"user_id": friend.user_id, "month": 2, "day": 30})
            assert status == 400 and body["errcode"] == "DARTCHAT_BAD_BIRTHDAY", (status, body)
            status, body = await api(http, "PUT", "/birthday", owner.access_token,
                                     {"month": 5, "day": 17})
            assert status == 400, (status, body)
            status, body = await api(http, "PUT", "/birthday", owner.access_token,
                                     {"user_id": friend.user_id, "month": 5, "day": 17})
            assert (status, body) == (200, {"month": 5, "day": 17}), (status, body)
            status, body = await api(http, "GET", "/birthdays", owner.access_token)
            assert (status, body) == (200, {"birthdays": {friend.user_id: {"month": 5, "day": 17}}}), (status, body)
            status, body = await api(http, "PUT", "/birthday", owner.access_token,
                                     {"user_id": friend.user_id})
            assert (status, body) == (200, {}), (status, body)

            room = await owner.room_create(
                name="Test grupa", invite=[friend.user_id],
                initial_state=[{"type": "m.room.encryption", "state_key": "",
                                "content": {"algorithm": "m.megolm.v1.aes-sha2"}}])
            room_id = room.room_id
            await until(clients, lambda: room_id in friend.invited_rooms, "the invite")
            async with http.post(f"{BASE}/_matrix/client/v3/rooms/{urllib.parse.quote(room_id, safe='')}/join", json={},
                                 headers={"Authorization": f"Bearer {friend.access_token}"}) as r:
                assert r.status == 200, await r.text()
            await until(clients, lambda: room_id in friend.rooms
                        and friend.user_id in owner.rooms[room_id].users, "the friend")
            path = "/rooms/" + urllib.parse.quote(room_id, safe="")

            status, body = await api(http, "PUT", path, friend.access_token,
                                     {"rules": {"repost": True}})
            assert status == 403, (status, body)
            status, body = await api(http, "PUT", path, owner.access_token,
                                     {"rules": {"repost": True, "commands": True}})
            assert status == 200 and body["rules"]["repost"], (status, body)
            await until(clients, lambda: bot_id in owner.rooms[room_id].users
                        and bot_id in friend.rooms[room_id].users, "the bot to join")
            status, body = await api(http, "GET", path, owner.access_token)
            assert body["bot_in_group"] and body["rules"]["commands"], body
            await asyncio.sleep(2)
            await step(clients)

            first = await send(friend, room_id, "pogledaj https://youtu.be/dQw4w9WgXcQ?si=abc")
            await asyncio.sleep(2)
            await send(owner, room_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s")

            def reply():
                return [e for r, e in friend.notices if r == room_id and "cosak" in e.body]

            await until(clients, reply, "the repost reply")
            event = reply()[0]
            relates = event.source["content"].get("m.relates_to", {})
            assert relates.get("m.in_reply_to", {}).get("event_id") == first, event.source
            assert "repostovao 1 put" in event.body, event.body

            await send(friend, room_id, "!lista")
            await until(clients, lambda: any("Lista ponavljača" in e.body
                                             for _, e in owner.notices), "the list")

            # The owner, and only the owner, can give the bot a picture.
            picture = f"mxc://{bot_id.split(':', 1)[1]}/abcDEF123"
            status, body = await api(http, "PUT", "/avatar", friend.access_token,
                                     {"avatar_url": picture})
            assert status == 403, (status, body)
            status, body = await api(http, "PUT", "/avatar", owner.access_token,
                                     {"avatar_url": "mxc://elsewhere.example/abc"})
            assert status == 400, (status, body)
            status, body = await api(http, "PUT", "/avatar", owner.access_token,
                                     {"avatar_url": picture})
            assert status == 200, (status, body)
            status, body = await api(http, "GET", "/status", owner.access_token)
            assert body["avatar_url"] == picture, body
            await until(clients, lambda: friend.rooms[room_id].avatar_url(bot_id) == picture,
                        "the bot's picture in the group")
            status, body = await api(http, "PUT", "/avatar", owner.access_token,
                                     {"avatar_url": ""})
            assert status == 200, (status, body)

            # After a restart the bot still knows its groups: the owner can
            # change the rules and replies are still encrypted.
            if RESTART:
                subprocess.run(RESTART, shell=True, check=True)
                await asyncio.sleep(3)
                await bot_ready(http, owner)
                status, body = await api(
                    http, "PUT", path, owner.access_token,
                    {"rules": {"repost": True, "commands": True, "daily_report": True}})
                assert status == 200 and body["rules"]["daily_report"], (status, body)
                assert body["bot_in_group"], body
                await send(friend, room_id, "https://x.com/a/status/42")
                await asyncio.sleep(2)
                await send(owner, room_id, "https://twitter.com/b/status/42?s=46")

                def second():
                    return [e for r, e in friend.notices
                            if r == room_id and "repostovao 2 puta" in e.body]

                await until(clients, second, "the reply after a restart")
                assert second()[0].decrypted, "the bot's reply was not encrypted"

            # Only the owner's ban and mute commands count.
            def said(text):
                return any(text in e.body for r, e in owner.notices if r == room_id)

            await send(friend, room_id, f"!mute @{OWNER} 1h")
            await until(clients, lambda: said("Samo vlasnik"), "the refusal")
            await send(owner, room_id, "!mute nobody 1h")
            await until(clients, lambda: said("Ne mogu da nađem"), "the unknown name")
            await send(owner, room_id, f"!mute @{FRIEND} 2h")
            await until(clients, lambda: said("ne može da piše"), "the mute")
            refused = await friend.room_send(
                room_id, "m.room.message", {"msgtype": "m.text", "body": "psst"},
                ignore_unverified_devices=True)
            assert not getattr(refused, "event_id", None), refused
            await send(owner, room_id, f"!unmute {FRIEND}")
            await until(clients, lambda: said("ponovo može da piše"), "the unmute")
            await send(friend, room_id, "hvala")

            if BRIDGE_TOKEN:
                await check_bridge(http, clients, owner, friend, room_id, path)

            status, body = await api(http, "PUT", path, owner.access_token,
                                     {"rules": {r: False for r in ALL_RULES + ["signal_bridge"]}})
            assert status == 200 and not any(body["rules"].values()), body
            await until(clients, lambda: bot_id not in owner.rooms[room_id].users,
                        "the bot to leave")
        for client in clients:
            await client.close()
    print("Group bot test passed")


if __name__ == "__main__":
    asyncio.run(main())
