"""Run inside the pinned official Core image to catch native LLM API drift."""

import asyncio
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import probatio

from homeassistant.components import websocket_api
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from custom_components.ha_control import ControlAPI
from custom_components.ha_control.catalog import catalog_command, describe_schema


async def main():
    with tempfile.TemporaryDirectory() as config_dir:
        hass = HomeAssistant(config_dir)
        hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(
                return_value=SimpleNamespace(is_admin=True, is_active=True)
            )
        )
        entry = SimpleNamespace(data={"addon_url": "http://companion:8213", "bridge_token": "test"})
        api = ControlAPI(hass, entry)
        assert api.requires_admin
        unregister = llm.async_register_api(hass, api)
        assert any(registered.id == "ha_control" for registered in llm.async_get_apis(hass))
        websocket_api.async_register_command(hass, catalog_command)
        assert len(hass.data[websocket_api.DOMAIN]["ha_control/catalog"]) == 2

        context = llm.LLMContext(
            platform="compat_check",
            context=Context(user_id="test-admin"),
            language="en",
            assistant="conversation",
            device_id=None,
        )
        instance = await api.async_get_api_instance(context)
        assert len(instance.tools) == 5
        assert {tool.name for tool in instance.tools} == {
            "ha_discover", "ha_request", "ha_files", "ha_events", "ha_result"
        }
        for tool in instance.tools:
            assert tool.integration == "ha_control"
            schema = probatio.to_openapi(tool.parameters)
            assert schema["type"] == "object"

        schema = describe_schema(catalog_command._ws_schema)
        assert schema["schema_complete"]
        assert schema["schema"]["properties"]["type"]["const"] == "ha_control/catalog"

        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={"ok": True, "data": {"items": []}})
        session = MagicMock()
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        session.post.return_value.__aexit__ = AsyncMock(return_value=None)
        with patch("custom_components.ha_control.async_get_clientsession", return_value=session):
            result = await instance.tools[0].async_call(
                hass,
                llm.ToolInput(tool_name="ha_discover", tool_args={"scope": "overview"}),
                context,
            )
        assert isinstance(result, llm.ToolResult)
        assert result.data == {"ok": True, "data": {"items": []}}

        hass.auth.async_get_user.return_value = SimpleNamespace(is_admin=False, is_active=True)
        try:
            await api.async_get_api_instance(context)
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("non-admin user was allowed")

        try:
            await api.async_get_api_instance(
                llm.LLMContext(
                    platform="compat_check", context=None, language="en",
                    assistant="conversation", device_id=None,
                )
            )
        except HomeAssistantError:
            pass
        else:
            raise AssertionError("missing user context was allowed")

        unregister()
        print(json.dumps({"core_llm_tools": len(instance.tools), "schemas": "valid",
                          "tool_result": "valid", "catalog_schema": "valid",
                          "api_registration": "valid", "admin_gate": "valid"}))


asyncio.run(main())
