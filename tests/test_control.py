import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path

from aiohttp import web
from mcp import types
from starlette.testclient import TestClient

from ha_control.common import CONTRACTS, MAX_OUTPUT, ControlError, Redactor, Results, encoded
from ha_control.engine import Engine
from ha_control.files import Files
from ha_control.server import create_app
from ha_control.bootstrap import bridge_identity
from ha_control.websocket import HASocket


class ResultTests(unittest.TestCase):
    def test_contract_budget(self):
        tools = [types.Tool(**c).model_dump(by_alias=True, exclude_none=True) for c in CONTRACTS]
        self.assertEqual(len(tools), 5)
        self.assertLessEqual(len(encoded({"tools": tools})), 8192)

    def test_pages_reconstruct_large_result_without_loss(self):
        store = Results(Redactor())
        data = [{"id": i, "state": "on"} for i in range(203)]
        page = store.pack(data)
        key = page["result_id"]
        output = []
        while True:
            self.assertLessEqual(len(encoded(page)), MAX_OUTPUT)
            output.extend(page["data"])
            if page["next_offset"] is None:
                break
            page = store.read(key, offset=page["next_offset"])
        self.assertEqual(output, data)

    def test_nested_large_values_remain_addressable(self):
        store = Results(Redactor())
        original = {"data": {"states": list(range(120)), "text": "Привет" * 9000}}
        page = store.pack(original)
        self.assertLessEqual(len(encoded(page)), MAX_OUTPUT)
        self.assertTrue(page["truncated"])
        text = store.read(page["result_id"], pointer="/data/text")
        self.assertLessEqual(len(encoded(text)), MAX_OUTPUT)
        self.assertEqual(store.read(page["result_id"], pointer="/data/states", offset=100)["data"], list(range(100, 120)))

    def test_expiry_and_eviction_are_explicit(self):
        now = [0]
        store = Results(Redactor(), ttl=10, clock=lambda: now[0], max_bytes=100)
        key = store.put([1, 2, 3])
        now[0] = 11
        with self.assertRaises(ControlError) as caught:
            store.read(key)
        self.assertEqual(caught.exception.code, "result_expired")
        self.assertEqual(store.bytes, 0)

    def test_credentials_redacted_before_storage(self):
        secret = "a-long-secret-value"
        store = Results(Redactor([secret]))
        key = store.put({"log": "token=" + secret, "password": "other", "rows": [secret] * 80})
        raw = encoded(store.items[key][1])
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(b'"other"', raw)


class FileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "config"
        self.root.mkdir()
        self.stopped = False
        async def core_stopped():
            return self.stopped
        self.files = Files({"homeassistant": self.root}, Path(self.tmp.name) / "revisions", core_stopped)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_preview_conflict_patch_and_restore(self):
        args = {"action": "write", "path": "homeassistant/example.yaml", "content": "value: 1\n", "expected_hash": "missing"}
        preview = await self.files.execute(**args)
        self.assertFalse(preview["applied"])
        self.assertFalse((self.root / "example.yaml").exists())
        created = await self.files.execute(**args, apply=True)
        changed = await self.files.execute("patch", "homeassistant/example.yaml", expected_hash=created["after_hash"], edits=[{"old": "value: 1", "new": "value: 2"}], apply=True)
        with self.assertRaises(ControlError):
            await self.files.execute(**args, apply=True)
        restored = await self.files.execute("restore", "homeassistant/example.yaml", expected_hash=changed["after_hash"], revision_id=changed["revision_id"], apply=True)
        self.assertEqual((self.root / "example.yaml").read_text(), "value: 1\n")
        await self.files.execute("restore", "homeassistant/example.yaml", expected_hash=restored["after_hash"], revision_id=created["revision_id"], apply=True)
        self.assertFalse((self.root / "example.yaml").exists())

    async def test_storage_requires_positive_stopped_evidence(self):
        (self.root / ".storage").mkdir()
        with self.assertRaises(ControlError) as caught:
            await self.files.execute("write", "homeassistant/.storage/test", content="{}", expected_hash="missing", apply=True)
        self.assertEqual(caught.exception.code, "core_running")
        self.stopped = True
        await self.files.execute("write", "homeassistant/.storage/test", content="{}", expected_hash="missing", apply=True)
        self.assertTrue((self.root / ".storage/test").exists())

    async def test_path_traversal_and_symlink_rejected(self):
        for path in ["homeassistant/../secret", "/etc/passwd", "homeassistant/./test"]:
            with self.assertRaises(ControlError):
                self.files.resolve(path)
        (self.root / "link").symlink_to("/etc/passwd")
        with self.assertRaises(ControlError):
            self.files.resolve("homeassistant/link")


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        async def handler(request):
            self.calls.append(request.path)
            if request.path == "/core/logs":
                if request.headers.get("Accept") not in {None, "text/plain", "text/x-log", "*/*"}:
                    return web.Response(status=400, text="Invalid content type requested. Only text/plain and text/x-log supported for now.")
                if request.headers.get("Authorization") != "Bearer test-secret":
                    raise web.HTTPUnauthorized()
                return web.Response(text="Core started\nprivate=test-secret\n", content_type="text/plain")
            if request.path == "/download":
                return web.Response(body=b"\x00\xff\x01", content_type="application/octet-stream")
            if request.path == "/slow":
                await asyncio.sleep(1.3)
            if request.path == "/redirect":
                raise web.HTTPFound("http://127.0.0.1:1/leak")
            return web.json_response({"path": request.path})
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.engine = Engine({"ha_url": f"http://127.0.0.1:{port}", "ha_token": "test-secret"}, self.tmp.name)
        await self.engine.start()

    async def asyncTearDown(self):
        await self.engine.close()
        await self.runner.cleanup()
        self.tmp.cleanup()

    async def test_mutation_timeout_never_retries_and_stops_batch(self):
        result = await self.engine.invoke("ha_request", {"requests": [
            {"transport": "rest", "operation": "/slow", "method": "POST", "timeout": 1},
            {"transport": "rest", "operation": "/after", "method": "POST"}]})
        self.assertTrue(result["data"][0]["error"]["uncertain"])
        self.assertEqual(result["data"][1]["error"]["code"], "not_executed")
        self.assertEqual(self.calls, ["/slow"])

    async def test_redirect_not_followed(self):
        result = await self.engine.invoke("ha_request", {"transport": "rest", "operation": "/redirect"})
        self.assertEqual(result["error"]["code"], "http_error")
        self.assertEqual(self.calls, ["/redirect"])

    async def test_batch_validates_before_side_effects(self):
        result = await self.engine.invoke("ha_request", {"requests": [
            {"transport": "rest", "operation": "/write", "method": "POST"}, {"invalid_key": True}]})
        self.assertFalse(result["ok"])
        self.assertEqual(self.calls, [])

    async def test_invalid_url_cannot_change_origin(self):
        for path in ["http://other/", "//other/", "/%2e%2e/secret"]:
            result = await self.engine.invoke("ha_request", {"transport": "rest", "operation": path})
            self.assertEqual(result["error"]["code"], "invalid_path")

    async def test_supervisor_text_logs_preserve_auth_and_redaction(self):
        self.engine.supervisor_url = self.engine.ha_url
        self.engine.supervisor_token = "test-secret"
        result = await self.engine.invoke("ha_request", {
            "transport": "supervisor", "operation": "/core/logs", "params": {"lines": 5}})
        self.assertTrue(result["ok"], result)
        self.assertIn("Core started", result["data"])
        self.assertNotIn("test-secret", result["data"])
        self.assertEqual(self.calls, ["/core/logs"])

    async def test_generic_http_still_decodes_json_and_binary(self):
        result = await self.engine.invoke("ha_request", {"requests": [
            {"transport": "rest", "operation": "/info"},
            {"transport": "rest", "operation": "/download"}]})
        self.assertTrue(all(item["ok"] for item in result["data"]))
        self.assertEqual(result["data"][0]["data"], {"path": "/info"})
        self.assertEqual(result["data"][1]["data"], {
            "content_type": "application/octet-stream", "base64": "AP8B"})


class AuthTests(unittest.TestCase):
    def test_bridge_identity_is_private_stable_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = bridge_identity(tmp)
            self.assertGreaterEqual(len(token), 32)
            self.assertEqual(bridge_identity(tmp), token)
            path = Path(tmp) / "bridge_token"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("broken")
            with self.assertRaises(ValueError):
                bridge_identity(tmp)

    def test_bridge_identity_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "other"
            target.write_text("x" * 40)
            (Path(tmp) / "bridge_token").symlink_to(target)
            with self.assertRaises(OSError):
                bridge_identity(tmp)

    def test_auth_separates_bridge_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app({"mcp_token": "m" * 40, "bridge_token": "b" * 40}, tmp)
            generated = bridge_identity(tmp)
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/bridge/tools").status_code, 401)
                self.assertEqual(client.get("/bridge/tools", headers={"Authorization": "Bearer " + "m" * 40}).status_code, 401)
                self.assertEqual(client.get("/bridge/tools", headers={"Authorization": "Bearer " + "b" * 40}).status_code, 401)
                self.assertEqual(client.get("/bridge/tools", headers={"Authorization": "Bearer " + generated}).status_code, 200)
                self.assertEqual(client.post("/mcp", json={}).status_code, 401)
                self.assertEqual(client.post("/mcp", json={}, headers={"Authorization": "Bearer " + generated}).status_code, 401)


class SupervisorBootstrapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_internal_routes_use_issued_token(self):
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "issued-token"}):
            engine = Engine({}, self.tmp.name)
        self.assertEqual(engine.ha_url, "http://supervisor/core")
        self.assertEqual(engine.ws_url, "ws://supervisor/core/websocket")
        self.assertEqual(engine.ha_token, "issued-token")

    async def test_discovery_retries_same_identity_without_user_mutation(self):
        engine = Engine({"bridge_token": "b" * 40}, self.tmp.name)
        info = {"data": {"hostname": "local-ha-control-mcp"}}
        engine._http = AsyncMock(side_effect=[info, ControlError("upstream_unavailable", "offline"), info, {"result": "ok"}])
        with patch("ha_control.engine.asyncio.sleep", new_callable=AsyncMock):
            await engine._publish_discovery()
        calls = engine._http.call_args_list
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1], calls[3])
        self.assertEqual(calls[1].kwargs["body"]["config"]["addon_url"], "http://local-ha-control-mcp:8213")
        self.assertEqual(calls[1].args, ("supervisor", "/discovery"))


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import aiohttp
        self.connections = []
        self.commands = []
        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            self.connections.append(ws)
            await ws.send_json({"type": "auth_required"})
            await ws.receive_json()
            await ws.send_json({"type": "auth_ok"})
            async for frame in ws:
                msg = json.loads(frame.data)
                self.commands.append(msg["type"])
                if msg["type"] == "lose_response":
                    await ws.close()
                    break
                if msg["type"] == "subscribe_events":
                    # The event can arrive before the subscription ACK.
                    await ws.send_json({"id": msg["id"], "type": "event", "event": {"data": {"entity_id": "test.one"}}})
                await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": {"received": msg["type"]}})
            return ws
        app = web.Application()
        app.router.add_get("/api/websocket", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.session = aiohttp.ClientSession()
        self.socket = HASocket(self.session, f"http://127.0.0.1:{port}/api/websocket", "test")

    async def asyncTearDown(self):
        await self.socket.close()
        await self.session.close()
        await self.runner.cleanup()

    async def test_lost_mutation_response_is_uncertain_without_retry(self):
        with self.assertRaises(ControlError) as caught:
            await self.socket.call("lose_response")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(self.commands.count("lose_response"), 1)

    async def test_early_events_overflow_and_reconnect_gaps(self):
        subscription = await self.socket.events("subscribe", entity_ids=["test.one"])
        key = subscription["subscription_id"]
        page = await self.socket.events("poll", subscription_id=key)
        self.assertEqual(page["events"][0]["kind"], "event")
        for i in range(300):
            self.socket._push(self.socket.subscriptions[key], {"kind": "event", "event": i})
        page = await self.socket.events("poll", subscription_id=key)
        self.assertIsNotNone(page["dropped_before_cursor"])
        self.assertEqual(len(self.socket.subscriptions[key]["queue"]), 256)
        await self.connections[-1].close()
        for _ in range(30):
            if self.socket.ws is None:
                break
            await asyncio.sleep(0.01)
        await self.socket.connect()
        page = await self.socket.events("poll", subscription_id=key, cursor=300)
        self.assertTrue(any(e["kind"] == "gap" for e in page["events"]))
        self.assertEqual(self.commands.count("subscribe_events"), 2)
        await self.socket.events("unsubscribe", subscription_id=key)
        self.assertNotIn(key, self.socket.subscriptions)


if __name__ == "__main__":
    unittest.main()
