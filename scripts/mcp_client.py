"""Small test/deployment client using the official SDK; never needed by agents."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def client(url=None):
    url = url or os.environ.get("HA_MCP_TEST_URL", "http://localhost:8213/mcp")
    options = json.loads((ROOT / ".local/options.json").read_text())
    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + options["mcp_token"]}, timeout=150, trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def call(session, name, args=None):
    result = await session.call_tool(name, args or {})
    data = json.loads(result.content[0].text)
    return data


async def main():
    import os
    async with client(os.environ.get("HA_MCP_TEST_URL", "http://localhost:8213/mcp")) as session:
        if len(sys.argv) == 1:
            tools = await session.list_tools()
            serialized = tools.model_dump(by_alias=True, exclude_none=True)
            print(json.dumps({"tools": [t.name for t in tools.tools], "schema_bytes": len(json.dumps(serialized, ensure_ascii=False, separators=(",", ":")).encode())}))
            print(json.dumps(await call(session, "ha_discover"), ensure_ascii=False))
        else:
            print(json.dumps(await call(session, sys.argv[1], json.loads(sys.stdin.read() or "{}")), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
