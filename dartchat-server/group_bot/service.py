"""The group-rules bot: a chat account on this server that applies the
owner's rules in the groups the owner chose.

Messages in Dart Chat are end-to-end encrypted, so the server itself cannot
read them. The bot is a member of each group that has rules switched on and
reads messages like any other member. It joins only the groups the owner
switched on, and leaves when every rule of a group is switched off.

It also serves the owner's endpoints under /_dartchat/v1/bot/ on
127.0.0.1:6169.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse

import aiohttp
from aiohttp import web
from nio import (
    AsyncClient,
    AsyncClientConfig,
    DummyEvent,
    InviteMemberEvent,
    LoginResponse,
    MegolmEvent,
    RedactionEvent,
    RoomEncryptedImage,
    RoomKeyRequest,
    RoomMessageImage,
    RoomMessageText,
)
from nio.crypto import attachments

from . import bridge
from .birthdays import Birthdays, valid as valid_birthday
from .core import Bot, Message, without_quote as core_without_quote
from . import moderation
from .ha import HASensors
from .rules import RULES, RoomRules, Settings, describe
from .store import Store

log = logging.getLogger("group_bot")

PREFIX = "/_dartchat/v1/bot"
SERVER_NAME = os.environ.get("DARTCHAT_SERVER_NAME", "")
HOMESERVER = os.environ.get("DARTCHAT_HOMESERVER", "http://127.0.0.1:6166")
REGISTRATION_TOKEN = os.environ.get("DARTCHAT_REGISTRATION_TOKEN", "")
OPTIONS = os.environ.get("DARTCHAT_OPTIONS", "/data/options.json")
DATA = os.environ.get("DARTCHAT_BOT_DIR", "/data/group_bot")
PORT = int(os.environ.get("DARTCHAT_BOT_PORT", "6169"))
BRIDGE_PORT = int(os.environ.get("DARTCHAT_BRIDGE_PORT", str(bridge.PORT)))
SERVICE = f"http://127.0.0.1:{os.environ.get('DARTCHAT_INVITE_PORT', '6168')}/_dartchat/v1"
USERNAME = "dartbot"
ROOM_ID = r"![A-Za-z0-9._~=+/-]+(?::[A-Za-z0-9.-]+(?::[0-9]+)?)?"


class ApiError(Exception):
    def __init__(self, status, errcode, message):
        super().__init__(message)
        self.status, self.errcode, self.message = status, errcode, message


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class Service:
    def __init__(self, http: aiohttp.ClientSession):
        self.http = http
        options = {}
        if os.path.exists(OPTIONS):
            with open(OPTIONS, encoding="utf-8") as handle:
                options = json.load(handle)
        self.settings = Settings(options)
        os.makedirs(os.path.join(DATA, "crypto"), exist_ok=True)
        self.rules = RoomRules(os.path.join(DATA, "rooms.json"))
        self.store = Store(os.path.join(DATA, "groups.sqlite3"))
        self.bot = Bot(self.settings, self.rules, self.store, self.send, http)
        self.bot.ready = False
        self.birthdays = Birthdays(os.path.join(DATA, "birthdays.json"))
        self.bot.birthdays = self.birthdays
        self.bot.members = self.members
        self.client: AsyncClient | None = None
        self.user_id = f"@{USERNAME}:{SERVER_NAME}"
        self.admin_room_id: str | None = None
        self.catching_up = True
        # The first sync after a start asks for every group's full state:
        # the saved position alone would leave the bot not knowing its groups
        # (who is in them, that they are encrypted) until something changes.
        self.first_sync = True
        self.senders: dict[str, str] = {}  # recent event id -> sender, for reply stats
        self.outbox = bridge.Outbox()
        # Recent event id -> (name, text) in bridged groups, to show what a
        # reply answers on the Signal side.
        self.bridged: dict[str, tuple[str, str]] = {}
        # Signal message (its timestamp) -> [(room_id, event_id, quote)] of our copies,
        # so an edit in Signal can edit them.
        self.from_signal_ids: dict[str, list] = {}
        # Groups where a phone told us it could not read the bot's key.
        self.reshare_rooms: set[str] = set()

    # -- talking to the chat server directly (not through the front door) --------
    async def chat(self, method, path, body=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with self.http.request(
                method, f"{HOMESERVER}/_matrix/client/v3{path}", json=body,
                headers=headers, timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}
                return resp.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise ApiError(502, "DARTCHAT_SERVER_DOWN",
                           "The chat server is not answering.") from error

    async def find_admin_room(self) -> str | None:
        status, body = await self.chat(
            "GET", f"/directory/room/{quote(f'#admins:{SERVER_NAME}')}")
        return body.get("room_id") if status == 200 else None

    # -- the bot's own account ------------------------------------------------
    async def account(self) -> dict:
        path = os.path.join(DATA, "credentials.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        # Wait for the owner's account first: the first account on a new
        # server becomes its admin, and that must not be the bot.
        while not await self.find_admin_room():
            log.info("Waiting for the owner's account before creating the bot account")
            await asyncio.sleep(30)
        password = secrets.token_urlsafe(24)
        request = {"username": USERNAME, "password": password,
                   "initial_device_display_name": "Group rules"}
        status, answer = await self.chat("POST", "/register", request)
        if status == 401 and "session" in answer:
            request["auth"] = {"type": "m.login.registration_token",
                               "token": REGISTRATION_TOKEN, "session": answer["session"]}
            status, answer = await self.chat("POST", "/register", request)
        if status != 200 or "access_token" not in answer:
            raise SystemExit(f"could not create the bot account: {status} "
                             f"{answer.get('errcode')} {answer.get('error')}")
        creds = {"user_id": answer["user_id"], "device_id": answer["device_id"],
                 "access_token": answer["access_token"], "password": password}
        descriptor = os.open(f"{path}.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(creds, handle)
        os.replace(f"{path}.tmp", path)
        log.info("Created the bot account %s", creds["user_id"])
        return creds

    async def connect(self) -> None:
        creds = await self.account()
        self.user_id = creds["user_id"]
        with open(os.path.join(DATA, "user_id"), "w", encoding="utf-8") as handle:
            handle.write(self.user_id)
        config = AsyncClientConfig(encryption_enabled=True, store_sync_tokens=True,
                                   max_timeout_retry_wait_time=60)
        client = AsyncClient(HOMESERVER, self.user_id, device_id=creds["device_id"],
                             store_path=os.path.join(DATA, "crypto"), config=config)
        client.restore_login(self.user_id, creds["device_id"], creds["access_token"])
        self.client = client
        self.catching_up = not (client.next_batch or client.loaded_sync_token)
        await client.set_displayname(self.settings.bot_name)
        client.add_event_callback(self.on_message, (RoomMessageText, RoomMessageImage,
                                                    RoomEncryptedImage))
        client.add_event_callback(self.on_undecryptable, MegolmEvent)
        client.add_event_callback(self.on_redaction, RedactionEvent)
        client.add_event_callback(self.on_invite, InviteMemberEvent)
        client.add_to_device_callback(self.on_dummy, DummyEvent)

    # -- events ---------------------------------------------------------------
    async def on_message(self, room, event) -> None:
        self.senders[event.event_id] = event.sender
        if len(self.senders) > 5000:
            self.senders.clear()
        if self.catching_up or event.sender == self.user_id:
            return
        content = event.source.get("content", {})
        relates = content.get("m.relates_to") or {}
        if relates.get("rel_type") == "m.replace":
            if self.bridge_on(room.room_id):
                await self.edit_to_signal(room, event, content, relates.get("event_id"))
            return  # edits are not checked for reposts
        if content.get("msgtype") == "m.notice":
            return  # other bots' notices
        reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
        message = Message(
            room_id=room.room_id, event_id=event.event_id, sender=event.sender,
            sender_name=room.user_name(event.sender) or event.sender[1:].split(":")[0],
            body=getattr(event, "body", "") or "", timestamp=event.server_timestamp,
            reply_to_sender=self.senders.get(reply_to) if reply_to else None,
            is_media=not isinstance(event, RoomMessageText),
        )
        try:
            if not message.is_media and await self.moderate(room, message):
                return
            if self.bridge_on(room.room_id):
                await self.to_signal(room, event, message, reply_to)
            await self.bot.handle(message)
        except Exception:
            log.exception("error handling a message")

    # -- the bridge to the owner's Signal group ------------------------------------
    def bridge_on(self, room_id: str) -> bool:
        return bool(self.settings.signal_bridge_token) and self.rules.on(room_id, "signal_bridge")

    def bridge_rooms(self) -> list[str]:
        return [r for r in self.rules.active_rooms() if self.bridge_on(r)]

    async def to_signal(self, room, event, message: Message, reply_to: str | None) -> None:
        """Queue a message from a bridged group for the Signal bot."""
        name = message.sender_name
        quote = None
        if reply_to and reply_to in self.bridged:
            who, said = self.bridged[reply_to]
            quote = {"name": who, "text": said}
        if message.is_media:
            data = await self.download_image(event)
            if data is None:
                return
            content = event.source.get("content", {})
            info = content.get("info") or {}
            body = (content.get("body") or "").strip()
            caption = "" if bridge.looks_like_filename(body) else body
            filename = body if bridge.looks_like_filename(body) else "photo.jpg"
            self.outbox.add(name, caption, bridge.image_item(
                data, info.get("mimetype") or getattr(event, "mimetype", "") or "image/jpeg",
                filename), quote)
            self.remember_bridged(message.event_id, name, caption or "📷")
        else:
            text = core_without_quote(message.body).strip()
            if not text:
                return
            self.outbox.add(name, text, quote=quote, event_id=message.event_id)
            self.remember_bridged(message.event_id, name, text)

    async def edit_to_signal(self, room, event, content: dict, original: str | None) -> None:
        """Someone edited a message in a bridged group: pass the new text on."""
        new = content.get("m.new_content") or {}
        text = (new.get("body") if isinstance(new, dict) else None) or content.get("body") or ""
        if not new and text.startswith("* "):
            text = text[2:]
        text = core_without_quote(text).strip()
        if not text or not original:
            return
        name = room.user_name(event.sender) or event.sender[1:].split(":")[0]
        self.outbox.add(name, text, edit_of=original)
        self.remember_bridged(original, name, text)

    def remember_bridged(self, event_id: str, name: str, text: str) -> None:
        if len(self.bridged) > 2000:
            self.bridged.clear()
        self.bridged[event_id] = (name, bridge.snippet(text))

    async def download_image(self, event) -> bytes | None:
        """The picture of an image message, decrypted."""
        url = getattr(event, "url", "") or ""
        match = re.fullmatch(r"mxc://([^/]+)/([^/?#]+)", url)
        if not match:
            return None
        try:
            async with self.http.get(
                f"{HOMESERVER}/_matrix/client/v1/media/download/{match[1]}/{quote(match[2])}",
                headers={"Authorization": f"Bearer {self.client.access_token}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    log.warning("bridge: could not download a picture: %s", resp.status)
                    return None
                data = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning("bridge: could not download a picture: %r", error)
            return None
        if isinstance(event, RoomEncryptedImage):
            try:
                data = attachments.decrypt_attachment(
                    data, event.key["k"], event.hashes["sha256"], event.iv)
            except Exception as error:
                log.warning("bridge: could not decrypt a picture: %r", error)
                return None
        return data

    async def upload_image(self, data: bytes, mime: str, filename: str) -> dict | None:
        """Encrypt and upload a picture; returns the "file" part of an image message."""
        ciphertext, keys = attachments.encrypt_attachment(data)
        try:
            async with self.http.post(
                f"{HOMESERVER}/_matrix/media/v3/upload",
                params={"filename": filename}, data=ciphertext,
                headers={"Authorization": f"Bearer {self.client.access_token}",
                         "Content-Type": "application/octet-stream"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                answer = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            log.warning("bridge: could not upload a picture: %r", error)
            return None
        address = (answer or {}).get("content_uri")
        if not address:
            log.warning("bridge: could not upload a picture: %s", answer)
            return None
        return {"url": address, **keys}

    async def from_signal(self, body: dict) -> int:
        """Post a message from the Signal group into every bridged group."""
        rooms = [r for r in self.bridge_rooms() if self.client and r in self.client.rooms]
        if not rooms:
            return 0
        name = str(body.get("name") or "Signal")[:80]
        text = str(body.get("text") or "")
        if body.get("edit_of") is not None:
            return await self.edit_from_signal(rooms, str(body["edit_of"]), name, text)
        signal_id = str(body["signal_ts"]) if body.get("signal_ts") is not None else None
        quote_ = body.get("quote") if isinstance(body.get("quote"), dict) else None
        pictures = []
        for image in (body.get("images") or [])[:10]:
            if not isinstance(image, dict):
                continue
            try:
                data = base64.b64decode(image.get("data") or "", validate=True)
            except (binascii.Error, ValueError):
                continue
            if data:
                pictures.append((data, image))
        if not text and not pictures:
            return 0
        delivered = 0
        for room_id in rooms:
            caption = bridge.signal_text(name, text, quote_)
            event_id = await self.post(room_id, {"msgtype": "m.text", "body": caption})
            sent = bool(event_id)
            if event_id:
                self.remember_bridged(event_id, name, text or "📷")
                if signal_id:
                    self.remember_signal(signal_id, room_id, event_id, quote_)
            for data, image in pictures:
                mime = str(image.get("mime") or "image/jpeg")
                filename = str(image.get("filename") or "photo.jpg")[:120]
                file = await self.upload_image(data, mime, filename)
                if not file:
                    sent = False
                    continue
                info = {"mimetype": mime, "size": len(data)}
                size = bridge.image_size(data)
                width, height = image.get("width"), image.get("height")
                if isinstance(width, int) and isinstance(height, int) and width and height:
                    size = (width, height)
                if size:
                    info["w"], info["h"] = size
                event_id = await self.post(room_id, {
                    "msgtype": "m.image", "body": filename, "info": info, "file": file})
                if event_id:
                    self.remember_bridged(event_id, name, "📷")
                sent = sent and bool(event_id)
            delivered += sent
        return delivered

    def remember_signal(self, signal_id: str, room_id: str, event_id: str, quote_) -> None:
        if len(self.from_signal_ids) > 2000:
            self.from_signal_ids.clear()
        self.from_signal_ids.setdefault(signal_id, []).append((room_id, event_id, quote_))

    async def edit_from_signal(self, rooms: list[str], signal_id: str, name: str,
                               text: str) -> int:
        """A Signal message was edited: edit our copy of it in each group."""
        if not text.strip():
            return 0
        known = {room: (event_id, quote_)
                 for room, event_id, quote_ in self.from_signal_ids.get(signal_id, [])}
        done = 0
        for room_id in rooms:
            if room_id in known:
                event_id, quote_ = known[room_id]
                caption = bridge.signal_text(name, text, quote_)
                ok = await self.post(room_id, {
                    "msgtype": "m.text", "body": f"* {caption}",
                    "m.new_content": {"msgtype": "m.text", "body": caption},
                    "m.relates_to": {"rel_type": "m.replace", "event_id": event_id}})
                if ok:
                    self.remember_bridged(event_id, name, text)
            else:  # sent before this bot last started: post the new text instead
                ok = await self.post(room_id, {"msgtype": "m.text",
                                               "body": f"✏️ {name}: {text}"})
            done += bool(ok)
        return done

    async def post(self, room_id: str, content: dict) -> str | None:
        content["dartchat.bridge"] = {"from": "signal"}
        try:
            await self.ensure_keys_shared(room_id)
            response = await self.client.room_send(room_id, "m.room.message", content,
                                                   ignore_unverified_devices=True)
            return getattr(response, "event_id", None)
        except Exception as error:
            log.warning("bridge: could not post: %r", error)
            return None

    # -- the owner's ban and mute commands ---------------------------------------
    async def service(self, method, path, body=None):
        try:
            async with self.http.request(
                method, f"{SERVICE}{path}", json=body or {},
                headers={"Authorization": f"Bearer {self.client.access_token}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ValueError:
                    data = {}
                return resp.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 502, {}

    async def moderate(self, room, message) -> bool:
        """Handles "!ban @name 1d" and friends. True when the message was one."""
        command = moderation.parse_command(message.body)
        if command is None:
            return False
        room_id, reply = message.room_id, message.event_id
        if command == "help":
            await self.send(room_id, moderation.HELP, reply)
            return True
        kind, start, name, minutes = command
        asked = {"requested_by": message.sender}
        if start:
            people = {user_id: user.display_name or ""
                      for user_id, user in room.users.items()}
            target = moderation.find_person(name, people)
            if target is None:
                await self.send(room_id, f"Ne mogu da nađem „{name}” u ovoj grupi.", reply)
                return True
            status, answer = await self.service("POST", "/moderation", {
                **asked, "room_id": room_id, "user_id": target, "kind": kind,
                "minutes": minutes})
            shown = people.get(target) or target[1:].split(":")[0]
        else:
            status, answer = await self.service("GET", "/moderation")
            entries = [e for e in answer.get("all", [])
                       if e.get("room_id") == room_id and e.get("kind") == kind]
            target = moderation.find_person(
                name, {e["user_id"]: room.user_name(e["user_id"]) or "" for e in entries})
            if status == 200 and target is None:
                await self.send(room_id, f"„{name}” nema {'zabranu' if kind == 'ban' else 'mute'} "
                                "u ovoj grupi.", reply)
                return True
            if status == 200:
                entry = next(e for e in entries if e["user_id"] == target)
                status, answer = await self.service(
                    "DELETE", f"/moderation/{entry['id']}", asked)
            shown = room.user_name(target) if target else name
            shown = shown or (target[1:].split(":")[0] if target else name)
        if status == 200:
            text = moderation.done_text(kind, start, shown, answer.get("until"),
                                        self.settings.tz)
        elif status == 403 and answer.get("errcode") == "M_FORBIDDEN":
            text = "Samo vlasnik može to da uradi."
        else:
            text = answer.get("error") or "Nije uspelo. Probaj ponovo."
        await self.send(room_id, text, reply)
        return True

    async def on_undecryptable(self, room, event) -> None:
        if self.catching_up or not self.rules.active(room.room_id):
            return
        log.info("could not read a message in a group yet; asking for its key")
        try:
            await self.client.request_room_key(event)
        except Exception:
            pass

    async def on_redaction(self, room, event) -> None:
        if event.redacts:
            self.bot.redacted(room.room_id, event.redacts)

    async def on_invite(self, room, event) -> None:
        if event.state_key != self.user_id or event.membership != "invite":
            return
        await self.on_invite_room(room.room_id)

    async def send(self, room_id: str, text: str, reply_to: str | None) -> bool:
        content = {"msgtype": "m.notice", "body": text}
        if reply_to:
            content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to}}
        if room_id not in self.client.rooms:
            # Without the group's state the bot cannot tell that it is
            # encrypted, and must not answer in the clear.
            log.warning("not answering in a group whose state is not loaded yet")
            self.first_sync = True
            return False
        try:
            await self.ensure_keys_shared(room_id)
            response = await self.client.room_send(room_id, "m.room.message", content,
                                                   ignore_unverified_devices=True)
            if getattr(response, "event_id", None):
                return True
            log.warning("could not send: %s", response)
        except Exception as e:
            log.warning("could not send: %r", e)
        return False

    # -- making sure every phone gets the key to the bot's messages ----------------
    def unshared_devices(self, room) -> set[tuple[str, str]]:
        """Devices in the group that don't have the key of the bot's current
        message session (for example because the chat server was restarting
        when the key was sent, or because someone signed in on a new phone)."""
        olm = self.client.olm
        session = olm.outbound_group_sessions.get(room.room_id)
        if session is None or session.expired:
            return set()  # a new session is shared with everyone anyway
        done = session.users_shared_with | session.users_ignored
        return {(user, device.id)
                for user in room.users
                for device in olm.device_store.active_user_devices(user)
                if device.id != self.client.device_id
                and (user, device.id) not in done
                and not olm.is_device_blacklisted(device)}

    async def ensure_keys_shared(self, room_id: str) -> None:
        """nio marks a message key as handed out even when sending it to some
        phones failed; those phones then show "Waiting for this message" for
        everything the bot writes. Hand the key again to whoever lacks it."""
        client = self.client
        room = client.rooms.get(room_id) if client else None
        if not room or not getattr(room, "encrypted", False) or not getattr(client, "olm", None):
            return
        if client.should_query_keys:
            await client.keys_query()
        missing: set = set()
        for attempt in range(3):
            missing = self.unshared_devices(room)
            if not missing:
                return
            if attempt:
                await asyncio.sleep(2 * attempt)
            client.olm.outbound_group_sessions[room_id].shared = False
            waiting = client.sharing_session.get(room_id)
            if waiting:
                await waiting.wait()
                continue
            try:
                await client.share_group_session(room_id, ignore_unverified_devices=True)
                log.info("handed the message key to %d phone(s) that lacked it", len(missing))
            except Exception as error:
                log.warning("could not hand out the message key: %r", error)
        missing = self.unshared_devices(room)
        if missing:
            log.warning("%d device(s) in a group still lack the message key", len(missing))

    async def on_dummy(self, event) -> None:
        """A phone couldn't decrypt what the bot sent it (its secure channel to
        the bot broke) and opened a new one. Whatever key went over the broken
        channel never arrived: send the current keys to that phone again."""
        olm = self.client.olm
        device = next((d for d in olm.device_store.active_user_devices(event.sender)
                       if d.curve25519 == event.sender_key), None)
        if device is None:
            return
        for room_id in self.rules.active_rooms():
            room = self.client.rooms.get(room_id)
            session = olm.outbound_group_sessions.get(room_id)
            if not room or event.sender not in room.users or session is None:
                continue
            session.users_shared_with.discard((event.sender, device.id))
            self.reshare_rooms.add(room_id)
        log.info("a phone could not read the bot's message key; sending it again")

    async def answer_key_requests(self, response) -> None:
        """A phone that missed the key of one of the bot's messages asks for
        it. nio only answers the bot's own devices, so answer group members'
        phones here: only for keys the bot made itself, and only for people
        who are in that group now."""
        olm = self.client.olm if self.client else None
        if not olm:
            return
        own_key = olm.account.identity_keys["curve25519"]
        for event in getattr(response, "to_device_events", []) or []:
            if not isinstance(event, RoomKeyRequest) or event.sender == self.user_id:
                continue
            room = self.client.rooms.get(event.room_id)
            # Newer apps leave out sender_key; the bot only hands out its own keys.
            if (not room or event.sender not in room.users
                    or not self.rules.active(event.room_id)
                    or (event.sender_key or own_key) != own_key):
                continue
            try:
                await self.forward_key(event, own_key)
            except Exception as error:
                log.warning("could not answer a key request: %r", error)

    async def forward_key(self, event, sender_key: str) -> None:
        olm = self.client.olm
        session = olm.inbound_group_store.get(event.room_id, sender_key, event.session_id)
        if session is None:
            return
        device = olm.device_store[event.sender].get(event.requesting_device_id) \
            if event.sender in olm.device_store.users else None
        if device is None:
            await self.client.keys_query()
            device = olm.device_store[event.sender].get(event.requesting_device_id) \
                if event.sender in olm.device_store.users else None
        if device is None or device.deleted or olm.is_device_blacklisted(device):
            return
        olm_session = olm.session_store.get(device.curve25519)
        if olm_session is None:
            await self.client.keys_claim({event.sender: [device.id]})
            olm_session = olm.session_store.get(device.curve25519)
        if olm_session is None:
            return
        message = olm._encrypt_forwarding_key(event.room_id, session, olm_session, device)
        await self.client.to_device(message)
        log.info("gave a phone the key to an earlier bot message")

    def members(self, room_id: str) -> dict[str, str]:
        """Who is in a group, with the name they show there."""
        room = self.client.rooms.get(room_id) if self.client else None
        if not room:
            return {}
        return {user: room.user_name(user) or user[1:].split(":")[0]
                for user in room.users if user != self.user_id}

    def room_name(self, room_id: str) -> str:
        room = self.client.rooms.get(room_id) if self.client else None
        return room.display_name if room else ""

    async def run_sync(self) -> None:
        while True:
            try:
                response = await self.client.sync(timeout=30000, full_state=self.first_sync)
                if hasattr(response, "next_batch"):
                    self.first_sync = False
                    await self.answer_key_requests(response)
                    for room_id in list(self.reshare_rooms):
                        self.reshare_rooms.discard(room_id)
                        try:
                            await self.ensure_keys_shared(room_id)
                        except Exception:
                            log.exception("could not send the message key again")
                    if self.client.should_upload_keys:
                        await self.client.keys_upload()
                    if self.client.should_query_keys:
                        await self.client.keys_query()
                    if self.client.should_claim_keys:
                        await self.client.keys_claim(self.client.get_users_for_key_claiming())
                    await self.client.send_to_device_messages()
                    self.catching_up = False
                    if not self.bot.ready:
                        await self.tidy_rooms()
                        self.bot.ready = True
                        log.info("Ready. Watching %d group(s)", len(self.rules.active_rooms()))
                else:
                    log.warning("sync failed: %s", response)
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sync error")
                await asyncio.sleep(10)

    async def tidy_rooms(self) -> None:
        """Join groups the owner switched on, and leave any others."""
        for room_id in list(self.client.invited_rooms):
            await self.on_invite_room(room_id)
        for room_id in await self.joined_rooms():
            if not self.rules.active(room_id):
                await self.leave(room_id)

    async def on_invite_room(self, room_id: str) -> None:
        if self.rules.active(room_id):
            await self.join(room_id)
        else:
            await self.leave(room_id, forget=False)

    # The chat server wants a JSON body on these, which nio leaves out.
    async def join(self, room_id: str) -> bool:
        status, answer = await self.chat("POST", f"/rooms/{quote(room_id)}/join", {},
                                         self.client.access_token)
        if status != 200:
            log.warning("could not join a group: %s %s", status, answer.get("errcode"))
        return status == 200

    async def leave(self, room_id: str, forget: bool = True) -> None:
        await self.chat("POST", f"/rooms/{quote(room_id)}/leave", {}, self.client.access_token)
        if forget:
            await self.chat("POST", f"/rooms/{quote(room_id)}/forget", {},
                            self.client.access_token)

    async def ticker(self) -> None:
        while True:
            try:
                await self.bot.tick()
            except Exception:
                log.exception("error in the midnight report or sensors")
            await asyncio.sleep(30)

    # -- the owner's endpoints --------------------------------------------------
    async def require_owner(self, request) -> tuple[str, str]:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) < 8:
            raise ApiError(401, "M_MISSING_TOKEN", "Sign in first.")
        token = header[7:].strip()
        status, body = await self.chat("GET", "/account/whoami", token=token)
        if status != 200 or "user_id" not in body:
            raise ApiError(401, "M_UNKNOWN_TOKEN", "Sign in again.")
        if self.admin_room_id is None:
            self.admin_room_id = await self.find_admin_room()
        status, joined = await self.chat("GET", "/joined_rooms", token=token)
        rooms = joined.get("joined_rooms", []) if status == 200 else []
        if not self.admin_room_id or self.admin_room_id not in rooms:
            raise ApiError(403, "M_FORBIDDEN", "Only the server owner can do this.")
        request["joined_rooms"] = rooms
        return body["user_id"], token

    # -- birthdays: only the owner enters them, for everyone ------------------------
    def birthday_of(self, user: str) -> dict:
        saved = self.birthdays.get(user)
        return {"month": saved[0], "day": saved[1]} if saved else {}

    async def api_get_birthdays(self, request):
        await self.require_owner(request)
        return {"birthdays": {user: {"month": day[0], "day": day[1]}
                              for user, day in self.birthdays.all().items()}}

    async def api_put_birthday(self, request):
        """{"user_id": "@ana:…", "month": 5, "day": 17} saves that person's
        birthday; without month and day it removes it."""
        await self.require_owner(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "M_NOT_JSON", "Send the birthday as a JSON object.")
        user = body.get("user_id")
        if not isinstance(user, str) or not user.startswith("@") or ":" not in user:
            raise ApiError(400, "M_INVALID_PARAM", "Say whose birthday it is.")
        month, day = body.get("month"), body.get("day")
        if month is None and day is None:
            self.birthdays.remove(user)
        elif valid_birthday(month, day):
            self.birthdays.set(user, month, day)
        else:
            raise ApiError(400, "DARTCHAT_BAD_BIRTHDAY", "That is not a day of the year.")
        return self.birthday_of(user)

    async def joined_rooms(self) -> set[str]:
        """The groups the bot is in, as the chat server has it."""
        if not self.client:
            return set()
        status, body = await self.chat("GET", "/joined_rooms", token=self.client.access_token)
        if status != 200:
            return set(self.client.rooms)
        return set(body.get("joined_rooms", []))

    def room_state(self, room_id: str, joined: set[str]) -> dict:
        return {"room_id": room_id, "rules": self.rules.get(room_id),
                "bot_in_group": room_id in joined}

    async def api_status(self, request):
        await self.require_owner(request)
        joined = await self.joined_rooms()
        return {"ready": self.bot.ready, "user_id": self.user_id,
                "name": self.settings.bot_name, "avatar_url": await self.avatar(),
                "rules": describe(self.settings),
                "groups": [dict(self.room_state(r, joined), name=self.room_name(r))
                           for r in self.rules.active_rooms()]}

    async def avatar(self) -> str:
        """The bot's profile picture (an mxc:// address), or "" for none."""
        if not self.client:
            return ""
        status, body = await self.chat(
            "GET", f"/profile/{quote(self.user_id)}/avatar_url", token=self.client.access_token)
        value = body.get("avatar_url") if status == 200 else None
        return value if isinstance(value, str) else ""

    async def api_put_avatar(self, request):
        """The owner gives the bot a profile picture they uploaded, or
        removes it with an empty address."""
        owner, _ = await self.require_owner(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        address = body.get("avatar_url") if isinstance(body, dict) else None
        if not isinstance(address, str) or (address and not re.fullmatch(
                r"mxc://" + re.escape(SERVER_NAME) + r"/[A-Za-z0-9_-]{1,255}", address)):
            raise ApiError(400, "M_INVALID_PARAM", "Send a picture uploaded to this server.")
        if not self.client:
            raise ApiError(503, "DARTCHAT_BOT_STARTING", "The group bot is starting. "
                           "Try again in a minute.")
        status, answer = await self.chat(
            "PUT", f"/profile/{quote(self.user_id)}/avatar_url", {"avatar_url": address},
            self.client.access_token)
        if status != 200:
            raise ApiError(502, "DARTCHAT_AVATAR_FAILED",
                           answer.get("error", "The picture could not be set."))
        log.info("%s %s the bot's picture", owner, "set" if address else "removed")
        return {"avatar_url": address}

    async def api_get_room(self, request):
        await self.require_owner(request)
        return self.room_state(request.match_info["room_id"], await self.joined_rooms())

    async def api_put_room(self, request):
        owner, token = await self.require_owner(request)
        room_id = request.match_info["room_id"]
        if room_id not in request["joined_rooms"]:
            raise ApiError(403, "M_FORBIDDEN", "You are not in that group.")
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("rules"), dict):
            raise ApiError(400, "M_NOT_JSON", "Send the rules as a JSON object.")
        if not self.client or not self.bot.ready:
            raise ApiError(503, "DARTCHAT_BOT_STARTING", "The group bot is starting. "
                           "Try again in a minute.")
        wanted = {r: bool(body["rules"].get(r)) for r in RULES}
        in_group = room_id in await self.joined_rooms()
        if any(wanted.values()) and not in_group:
            status, answer = await self.chat(
                "POST", f"/rooms/{quote(room_id)}/invite", {"user_id": self.user_id}, token)
            if status != 200 and "already" not in str(answer.get("error", "")).lower():
                if status == 403:
                    raise ApiError(403, "DARTCHAT_NOT_GROUP_ADMIN",
                                   "Only a group admin can add the group bot.")
                raise ApiError(502, "DARTCHAT_INVITE_FAILED",
                               answer.get("error", "The bot could not be added."))
            self.rules.set(room_id, wanted)
            await self.join(room_id)
        else:
            self.rules.set(room_id, wanted)
            if not any(wanted.values()) and in_group:
                await self.leave(room_id)
        log.info("%s set group rules: %s", owner,
                 ", ".join(r for r, on in wanted.items() if on) or "none")
        return self.room_state(room_id, await self.joined_rooms())

    # -- the Signal bot's endpoints (Home Assistant's internal network) ------------
    def require_bridge(self, request) -> None:
        token = self.settings.signal_bridge_token
        header = request.headers.get("Authorization", "")
        given = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or not secrets.compare_digest(given.encode(), token.encode()):
            raise ApiError(401, "M_UNKNOWN_TOKEN", "Wrong bridge password.")

    async def api_bridge_status(self, request):
        self.require_bridge(request)
        return {"on": bool(self.bridge_rooms()), "ready": self.bot.ready}

    async def api_bridge_outbox(self, request):
        self.require_bridge(request)
        try:
            cursor = int(request.query.get("after", "0"))
            wait = min(max(float(request.query.get("wait", "0")), 0), bridge.MAX_WAIT)
        except ValueError:
            raise ApiError(400, "M_INVALID_PARAM", "after and wait are numbers.")
        if not self.bridge_rooms():
            self.outbox.clear()
        items = await self.outbox.wait(cursor, wait)
        return {"items": items, "last": self.outbox.last, "on": bool(self.bridge_rooms())}

    async def api_bridge_inbox(self, request):
        self.require_bridge(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise ApiError(400, "M_NOT_JSON", "Send the message as a JSON object.")
        if not self.client or not self.bot.ready:
            raise ApiError(503, "DARTCHAT_BOT_STARTING", "The group bot is starting.")
        delivered = await self.from_signal(body)
        return {"delivered": delivered, "on": bool(self.bridge_rooms())}

    def bridge_app(self) -> web.Application:
        def wrap(handler):
            async def run(request):
                try:
                    return web.json_response(await handler(request))
                except ApiError as error:
                    return web.json_response({"errcode": error.errcode, "error": error.message},
                                             status=error.status)
                except Exception:
                    log.exception("unexpected error on %s %s", request.method, request.path)
                    return web.json_response({"errcode": "M_UNKNOWN",
                                              "error": "Something went wrong."}, status=500)
            return run

        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get(f"{bridge.PREFIX}/status", wrap(self.api_bridge_status))
        app.router.add_get(f"{bridge.PREFIX}/outbox", wrap(self.api_bridge_outbox))
        app.router.add_post(f"{bridge.PREFIX}/inbox", wrap(self.api_bridge_inbox))
        return app

    def app(self) -> web.Application:
        def wrap(handler):
            async def run(request):
                try:
                    return web.json_response(await handler(request),
                                             headers={"Cache-Control": "no-store"})
                except ApiError as error:
                    return web.json_response({"errcode": error.errcode, "error": error.message},
                                             status=error.status)
                except Exception:
                    log.exception("unexpected error on %s %s", request.method, request.path)
                    return web.json_response({"errcode": "M_UNKNOWN",
                                              "error": "Something went wrong."}, status=500)
            return run

        app = web.Application(client_max_size=16384)
        app.router.add_get(f"{PREFIX}/status", wrap(self.api_status))
        app.router.add_put(f"{PREFIX}/avatar", wrap(self.api_put_avatar))
        app.router.add_get(f"{PREFIX}/birthdays", wrap(self.api_get_birthdays))
        app.router.add_put(f"{PREFIX}/birthday", wrap(self.api_put_birthday))
        app.router.add_get(PREFIX + "/rooms/{room_id:" + ROOM_ID + "}", wrap(self.api_get_room))
        app.router.add_put(PREFIX + "/rooms/{room_id:" + ROOM_ID + "}", wrap(self.api_put_room))
        return app


async def wait_for_server(http: aiohttp.ClientSession) -> None:
    while True:
        try:
            async with http.get(f"{HOMESERVER}/_matrix/client/versions",
                                timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(3)


async def run() -> int:
    if not SERVER_NAME or not REGISTRATION_TOKEN:
        log.error("FATAL: server name or registration token missing")
        return 78
    async with aiohttp.ClientSession() as http:
        service = Service(http)
        runner = web.AppRunner(service.app(), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", PORT).start()
        if service.settings.signal_bridge_token:
            # Reachable only inside Home Assistant (no port is published).
            bridge_runner = web.AppRunner(service.bridge_app(), access_log=None)
            await bridge_runner.setup()
            await web.TCPSite(bridge_runner, "0.0.0.0", BRIDGE_PORT).start()
            log.info("Signal bridge listening on port %d", BRIDGE_PORT)
        await wait_for_server(http)
        await service.connect()
        sensors = HASensors(http, service.bot, service.room_name)
        if sensors.enabled:
            service.bot.sensors = sensors
        tasks = [asyncio.create_task(service.ticker())]
        try:
            await service.run_sync()
        finally:
            for task in tasks:
                task.cancel()
            await service.client.close()
    return 0


def main() -> int:
    logging.basicConfig(level=os.environ.get("DARTCHAT_BOT_LOG", "INFO"),
                        format="[dartchat-bot] %(levelname)s %(message)s", stream=sys.stdout)
    for noisy in ("nio", "peewee", "aiohttp.access"):  # chatty libraries
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
