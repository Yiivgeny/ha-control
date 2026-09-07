"""Prove that the MCP control plane survives Core stop/start; always restart Core."""

import asyncio
import json
from pathlib import Path
import uuid

from acceptance import materialize
from mcp_client import client, call


async def main():
    report = {}
    path = "homeassistant/.storage/ha_control_acceptance_" + uuid.uuid4().hex[:8]
    file_hash = None
    async with client() as session:
        async def invoke(name, **kwargs):
            return await materialize(session, await call(session, name, kwargs))
        async def supervisor(operation):
            return await invoke("ha_request", transport="supervisor", operation=operation, method="POST", body={}, timeout=120)
        initial = await invoke("ha_discover")
        original_version = initial["core"]["version"]
        subscription = await invoke("ha_events", action="subscribe")
        key = subscription["subscription_id"]
        try:
            await supervisor("/core/stop")
            report["core_stop_acknowledged"] = True
            print("Core stopped through MCP", flush=True)
            overview = await invoke("ha_discover")
            assert overview["core"].get("ok") is False
            info = await invoke("ha_request", transport="supervisor", operation="/info")
            assert info["result"] == "ok"
            files = await invoke("ha_files", action="list", path="homeassistant")
            assert any(f["name"] == "configuration.yaml" for f in files)
            report["supervisor_and_files_available_without_core"] = True
            created = await invoke("ha_files", action="write", path=path, content='{"temporary":true}\n', expected_hash="missing", apply=True)
            file_hash = created["after_hash"]
            read = await invoke("ha_files", action="read", path=path)
            assert json.loads(read["content"])["temporary"] is True
            await invoke("ha_files", action="restore", path=path, revision_id=created["revision_id"], expected_hash=file_hash, apply=True)
            file_hash = None
            report["storage_edit_and_restore_while_stopped"] = True
            print("Supervisor, config and guarded .storage recovery passed", flush=True)
        finally:
            if file_hash:
                await invoke("ha_files", action="delete", path=path, expected_hash=file_hash, apply=True)
            await supervisor("/core/start")
            print("Core start acknowledged", flush=True)
        for _ in range(40):
            overview = await invoke("ha_discover")
            if overview["core"].get("version"):
                break
            await asyncio.sleep(2)
        assert overview["core"].get("version") == original_version, overview
        report["core_recovered"] = True
        events = await invoke("ha_events", action="poll", subscription_id=key)
        assert any(e["kind"] == "gap" for e in events["events"]), events
        await invoke("ha_events", action="unsubscribe", subscription_id=key)
        report["subscription_reconnected_with_gap_marker"] = True
        catalog = await invoke("ha_discover", scope="websocket", query="registry")
        assert catalog["llm_api_registered"] is True
        report["native_llm_api_registered"] = True
        schema = await invoke("ha_discover", scope="websocket", operation="input_number/create")
        assert "id" not in schema["schema"]["properties"] and "type" not in schema["schema"]["properties"]
        report["wire_fields_not_exposed_as_user_arguments"] = True
        Path(".local/resilience.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
