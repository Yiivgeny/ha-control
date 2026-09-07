"""Official MCP SDK adapter plus a private adapter for HA llm.Tool calls."""

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os

from mcp import types
from mcp.server import Server
from starlette.responses import JSONResponse
from starlette.routing import Route
import uvicorn

from . import __version__
from .common import CONTRACTS
from .engine import Engine
from .bootstrap import bridge_identity


class BearerMiddleware:
    def __init__(self, app, mcp_token, bridge_token):
        self.app, self.mcp_token, self.bridge_token = app, mcp_token, bridge_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] == "/health":
            return await self.app(scope, receive, send)
        expected = self.bridge_token if scope["path"].startswith("/bridge/") else self.mcp_token
        headers = dict(scope.get("headers", []))
        given = headers.get(b"authorization", b"")
        if not hmac.compare_digest(given, ("Bearer " + expected).encode()):
            response = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(options, data_dir="/data", roots=None):
    if len(options.get("mcp_token", "")) < 32:
        raise ValueError("Configure an MCP token of at least 32 characters.")
    options = {**options, "bridge_token": bridge_identity(data_dir)}
    engine = Engine(options, data_dir, roots)

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(**c) for c in CONTRACTS])

    async def call_tool(ctx, params):
        result = await engine.invoke(params.name, params.arguments or {})
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))], is_error=not result["ok"])

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__})

    async def bridge_tools(request):
        return JSONResponse({"tools": CONTRACTS, "version": __version__})

    async def bridge_call(request):
        try:
            if int(request.headers.get("content-length", "0")) > 12 * 1024 * 1024:
                return JSONResponse({"error": "body_too_large"}, status_code=413)
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 12 * 1024 * 1024:
                    return JSONResponse({"error": "body_too_large"}, status_code=413)
            payload = json.loads(raw)
            result = await engine.invoke(payload["name"], payload.get("arguments", {}))
            return JSONResponse(result)
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    server = Server("ha-control", version=__version__,
                    instructions="Control the connected Home Assistant instance. Discover narrow schemas before calling generic operations. Use ha_result for large outputs. Do not repeat uncertain mutations.",
                    on_list_tools=list_tools, on_call_tool=call_tool)
    app = server.streamable_http_app(host="0.0.0.0", json_response=True,
        max_request_body_size=12 * 1024 * 1024,
        custom_starlette_routes=[Route("/health", health), Route("/bridge/tools", bridge_tools), Route("/bridge/call", bridge_call, methods=["POST"])])
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        await engine.start()
        try:
            async with original_lifespan(application):
                yield
        finally:
            await engine.close()

    app.router.lifespan_context = lifespan
    app.state.engine = engine
    app.add_middleware(BearerMiddleware, mcp_token=options["mcp_token"], bridge_token=options["bridge_token"])
    return app


if __name__ == "__main__":
    with open(os.environ.get("HA_CONTROL_OPTIONS", "/data/options.json")) as f:
        options = json.load(f)
    # Discard legacy 0.1.0 upstream credentials during an in-place upgrade.
    # The deployed app always uses Supervisor, never a saved LAN Core URL.
    for legacy in ("ha_url", "ha_token", "bridge_token"):
        options.pop(legacy, None)
    app = create_app(options, os.environ.get("HA_CONTROL_DATA", "/data"))
    uvicorn.run(app, host="0.0.0.0", port=8213, access_log=False, log_level="warning")
