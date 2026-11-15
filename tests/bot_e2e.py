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
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

import aiohttp
from nio import AsyncClient, AsyncClientConfig, RoomMessageNotice

BASE = sys.argv[1]
OWNER, OWNER_PASSWORD, FRIEND, FRIEND_PASSWORD = sys.argv[2:6]
BOT_API = (sys.argv[6] if len(sys.argv) > 6 else BASE) + "/_dartchat/v1/bot"
# A shell command that restarts the bot, to test that it survives one.
RESTART = os.environ.get("BOT_RESTART_CMD", "")
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

            status, body = await api(http, "PUT", path, owner.access_token,
                                     {"rules": {r: False for r in ALL_RULES}})
            assert status == 200 and not any(body["rules"].values()), body
            await until(clients, lambda: bot_id not in owner.rooms[room_id].users,
                        "the bot to leave")
        for client in clients:
            await client.close()
    print("Group bot test passed")


if __name__ == "__main__":
    asyncio.run(main())
