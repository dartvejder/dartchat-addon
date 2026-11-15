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
import json
import logging
import os
import secrets
import sys
import time
import urllib.parse

import aiohttp
from aiohttp import web
from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    LoginResponse,
    MegolmEvent,
    RedactionEvent,
    RoomEncryptedImage,
    RoomMessageImage,
    RoomMessageText,
)

from .core import Bot, Message
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
        self.client: AsyncClient | None = None
        self.user_id = f"@{USERNAME}:{SERVER_NAME}"
        self.admin_room_id: str | None = None
        self.catching_up = True
        self.senders: dict[str, str] = {}  # recent event id -> sender, for reply stats

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

    # -- events ---------------------------------------------------------------
    async def on_message(self, room, event) -> None:
        self.senders[event.event_id] = event.sender
        if len(self.senders) > 5000:
            self.senders.clear()
        if self.catching_up or event.sender == self.user_id:
            return
        content = event.source.get("content", {})
        relates = content.get("m.relates_to") or {}
        if relates.get("rel_type") == "m.replace" or content.get("msgtype") == "m.notice":
            return  # edits and other bots' notices
        reply_to = (relates.get("m.in_reply_to") or {}).get("event_id")
        message = Message(
            room_id=room.room_id, event_id=event.event_id, sender=event.sender,
            sender_name=room.user_name(event.sender) or event.sender[1:].split(":")[0],
            body=getattr(event, "body", "") or "", timestamp=event.server_timestamp,
            reply_to_sender=self.senders.get(reply_to) if reply_to else None,
            is_media=not isinstance(event, RoomMessageText),
        )
        try:
            await self.bot.handle(message)
        except Exception:
            log.exception("error handling a message")

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
        try:
            response = await self.client.room_send(room_id, "m.room.message", content,
                                                   ignore_unverified_devices=True)
            if getattr(response, "event_id", None):
                return True
            log.warning("could not send: %s", response)
        except Exception as e:
            log.warning("could not send: %r", e)
        return False

    def room_name(self, room_id: str) -> str:
        room = self.client.rooms.get(room_id) if self.client else None
        return room.display_name if room else ""

    async def run_sync(self) -> None:
        while True:
            try:
                response = await self.client.sync(timeout=30000, full_state=self.catching_up)
                if hasattr(response, "next_batch"):
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
        for room_id in list(self.client.rooms):
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

    def room_state(self, room_id: str) -> dict:
        joined = bool(self.client and room_id in self.client.rooms)
        return {"room_id": room_id, "rules": self.rules.get(room_id), "bot_in_group": joined}

    async def api_status(self, request):
        await self.require_owner(request)
        return {"ready": self.bot.ready, "user_id": self.user_id,
                "name": self.settings.bot_name, "rules": describe(self.settings),
                "groups": [dict(self.room_state(r), name=self.room_name(r))
                           for r in self.rules.active_rooms()]}

    async def api_get_room(self, request):
        await self.require_owner(request)
        return self.room_state(request.match_info["room_id"])

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
        if any(wanted.values()) and room_id not in self.client.rooms:
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
            if not any(wanted.values()) and room_id in self.client.rooms:
                await self.leave(room_id)
        log.info("%s set group rules: %s", owner,
                 ", ".join(r for r, on in wanted.items() if on) or "none")
        return self.room_state(room_id)

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
