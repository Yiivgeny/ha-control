"""Run inside the HA image: exercise native llm.Tool against the real companion.

Uses a separate HomeAssistant object and mocked identity lookup; never modifies
the running Core's users or config. The network operation is read-only.
"""

import asyncio
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from homeassistant.core import HomeAssistant, Context
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from custom_components.ha_control import ControlAPI
from custom_components.ha_control.config_flow import HAControlConfigFlow


async def main():
    entries = json.load(open("/config/.storage/core.config_entries"))["data"]["entries"]
    entry = next(e for e in entries if e["domain"] == "ha_control")
    with tempfile.TemporaryDirectory() as tmp:
        hass = HomeAssistant(tmp)
        hass.auth = SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=True, is_active=True)))
        api = ControlAPI(hass, SimpleNamespace(data=entry["data"]))
        context = llm.LLMContext(platform="ha_control_test", context=Context(user_id="test-admin"), language="ru", assistant=None, device_id=None)
        session = aiohttp.ClientSession()
        try:
            instance = await api.async_get_api_instance(context)
            tool = next(t for t in instance.tools if t.name == "ha_discover")
            with patch("custom_components.ha_control.async_get_clientsession", return_value=session):
                result = await tool.async_call(hass, llm.ToolInput(tool_name="ha_discover", tool_args={"scope": "overview"}), context)
            assert result["ok"] and result["data"]["core"]["version"]
            hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=False, is_active=True)
            try:
                await api.async_get_api_instance(context)
            except HomeAssistantError:
                denied = True
            else:
                denied = False
            assert denied
            flow = HAControlConfigFlow()
            flow.hass = hass
            foreign = await flow._connect(entry["data"], "some_other_addon")
            assert foreign["reason"] == "invalid_server"
            wrong_url = await flow._connect({**entry["data"], "addon_url": "http://untrusted:8213"}, "local_ha_control_mcp")
            assert wrong_url["reason"] == "invalid_server"
            flow.async_set_unique_id = AsyncMock()
            updates = []
            flow._abort_if_unique_id_configured = lambda **kwargs: updates.append(kwargs)
            with patch("custom_components.ha_control.config_flow.async_get_clientsession", return_value=session):
                paired = await flow._connect(entry["data"], entry["data"].get("addon_slug", "local_ha_control_mcp"))
            assert paired["type"] == "create_entry" and updates[0]["updates"]["bridge_token"] == entry["data"]["bridge_token"]
            print(json.dumps({"native_llm_tools": len(instance.tools), "bridge_roundtrip": True, "non_admin_denied": True,
                "discovery_validated": True, "foreign_addon_and_url_denied": True}))
        finally:
            await session.close()


asyncio.run(main())
