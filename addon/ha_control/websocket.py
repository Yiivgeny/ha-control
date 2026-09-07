"""One multiplexed HA socket, with explicit uncertain outcomes and bounded streams."""

import asyncio
import time
import uuid
from collections import deque

import aiohttp

from .common import ControlError


class HASocket:
    def __init__(self, session, url, token):
        self.session, self.url, self.token = session, url, token
        self.ws = None
        self.reader = None
        self.reconnector = None
        self.lock = asyncio.Lock()
        self.next_id = 0
        self.pending = {}
        self.subscriptions = {}
        self.wire_subscriptions = {}
        self.closed = False

    async def connect(self):
        async with self.lock:
            if self.ws is not None and not self.ws.closed:
                return
            try:
                ws = await self.session.ws_connect(self.url, heartbeat=30, max_msg_size=32 * 1024 * 1024, timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5))
                await asyncio.wait_for(ws.receive_json(), 10)
                await ws.send_json({"type": "auth", "access_token": self.token})
                auth = await asyncio.wait_for(ws.receive_json(), 10)
                if auth.get("type") != "auth_ok":
                    await ws.close()
                    raise ControlError("ha_auth_failed", "Home Assistant rejected the configured token.")
            except ControlError:
                raise
            except Exception:
                raise ControlError("core_unavailable", "Home Assistant WebSocket is unavailable.") from None
            self.ws = ws
            self.wire_subscriptions.clear()
            self.reader = asyncio.create_task(self._read(ws))
            for key, sub in list(self.subscriptions.items()):
                if time.monotonic() - sub["touched"] > 900:
                    self.subscriptions.pop(key)
                    continue
                self._push(sub, {"kind": "gap", "reason": "reconnected; events during disconnection may be missing"})
                try:
                    await self._send(sub["command"], subscription_id=key)
                except ControlError as exc:
                    self._push(sub, {"kind": "error", "error": exc.as_dict()["error"]})

    async def _read(self, ws):
        try:
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = frame.json()
                wire_id = data.get("id")
                if data.get("type") == "event" and wire_id in self.wire_subscriptions:
                    sub = self.subscriptions.get(self.wire_subscriptions[wire_id])
                    if sub is not None:
                        event = data.get("event")
                        ids = sub["entity_ids"]
                        if not ids or (isinstance(event, dict) and event.get("data", {}).get("entity_id") in ids):
                            self._push(sub, {"kind": "event", "event": event})
                elif wire_id in self.pending:
                    future = self.pending.pop(wire_id)
                    if not future.done():
                        future.set_result(data)
        except (aiohttp.ClientError, ValueError):
            pass
        finally:
            if self.ws is ws:
                self.ws = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ControlError("connection_lost", "Socket disconnected after sending; inspect state before retrying.", uncertain=True))
                self.pending.clear()
                for sub in self.subscriptions.values():
                    self._push(sub, {"kind": "gap", "reason": "connection_lost"})
                if not self.closed and self.subscriptions and (self.reconnector is None or self.reconnector.done()):
                    self.reconnector = asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        delay = 1
        while not self.closed and self.subscriptions:
            await asyncio.sleep(delay)
            try:
                await self.connect()
                return
            except ControlError:
                delay = min(30, delay * 2)

    async def _send(self, command, timeout=30, subscription_id=None):
        self.next_id += 1
        wire_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[wire_id] = future
        if subscription_id:
            self.wire_subscriptions[wire_id] = subscription_id
            self.subscriptions[subscription_id]["wire_id"] = wire_id
        try:
            await self.ws.send_json({**command, "id": wire_id})
            response = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise ControlError("timeout", "No response after sending; inspect state before retrying.", uncertain=True) from None
        except (aiohttp.ClientError, AttributeError, ConnectionError):
            raise ControlError("connection_lost", "Connection lost while sending; inspect state before retrying.", uncertain=True) from None
        finally:
            self.pending.pop(wire_id, None)
        if response.get("success") is False:
            if subscription_id:
                self.wire_subscriptions.pop(wire_id, None)
            raise ControlError("ha_error", response.get("error", {}).get("message", "HA command failed"), details=response.get("error"))
        return response.get("result")

    async def call(self, operation, params=None, timeout=30):
        if operation in {"auth", "auth_required", "auth_ok"}:
            raise ControlError("invalid_operation", "Authentication is managed by the server.")
        params = params or {}
        if "id" in params or "type" in params:
            raise ControlError("invalid_params", "Pass command type as operation; IDs are assigned by the server.")
        await self.connect()
        return await self._send({"type": operation, **params}, timeout)

    def _push(self, sub, event):
        sub["sequence"] += 1
        sub["queue"].append({"cursor": sub["sequence"], **event})

    async def events(self, action, subscription_id=None, event_type="state_changed", entity_ids=None, operation=None, params=None, cursor=0, limit=50):
        for key, sub in list(self.subscriptions.items()):
            if time.monotonic() - sub["touched"] > 900:
                await self._remove(key)
        if action == "subscribe":
            if len(self.subscriptions) >= 32:
                raise ControlError("subscription_limit", "At most 32 active subscriptions are allowed.")
            operation = operation or "subscribe_events"
            if not (operation.startswith("subscribe_") or operation.endswith("/subscribe")):
                raise ControlError("invalid_subscription", "Use a native subscription command; regular commands belong in ha_request.")
            command = {"type": operation, **(params or ({"event_type": event_type} if operation == "subscribe_events" else {}))}
            if "id" in command or command["type"] != operation:
                raise ControlError("invalid_params", "Subscription type and IDs are managed by the server.")
            await self.connect()
            key = uuid.uuid4().hex
            self.subscriptions[key] = {"command": command, "entity_ids": set(entity_ids or []),
                "queue": deque(maxlen=256), "sequence": 0, "touched": time.monotonic(), "wire_id": None}
            try:
                initial = await self._send(command, subscription_id=key)
            except BaseException:
                self.subscriptions.pop(key, None)
                raise
            return {"subscription_id": key, "cursor": 0, "initial": initial, "capacity": 256, "ttl": 900}
        if subscription_id not in self.subscriptions:
            raise ControlError("subscription_missing", "Subscription does not exist or has expired.")
        if action == "unsubscribe":
            await self._remove(subscription_id)
            return {"unsubscribed": subscription_id}
        sub = self.subscriptions[subscription_id]
        sub["touched"] = time.monotonic()
        records = [x for x in sub["queue"] if x["cursor"] > cursor][:limit]
        first = sub["queue"][0]["cursor"] if sub["queue"] else sub["sequence"] + 1
        return {"subscription_id": subscription_id, "events": records,
                "cursor": records[-1]["cursor"] if records else cursor,
                "dropped_before_cursor": first - 1 if cursor < first - 1 else None,
                "connected": self.ws is not None and not self.ws.closed}

    async def _remove(self, key):
        sub = self.subscriptions.pop(key)
        wire_id = sub["wire_id"]
        self.wire_subscriptions.pop(wire_id, None)
        if self.ws is not None and wire_id is not None:
            try:
                await self._send({"type": "unsubscribe_events", "subscription": wire_id}, timeout=5)
            except ControlError:
                pass

    async def close(self):
        self.closed = True
        if self.reconnector:
            self.reconnector.cancel()
        if self.ws:
            await self.ws.close()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
