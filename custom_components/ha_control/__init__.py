"""Native HA LLM API adapter; all execution delegates to the companion add-on."""

import asyncio
import importlib
import json
from pathlib import Path

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession

import aiohttp
import voluptuous as vol

from .catalog import catalog_command

DOMAIN = "ha_control"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CONTRACTS = json.loads(Path(__file__).with_name("contracts.json").read_text())
PARAMETER_SCHEMA = (
    vol
    if isinstance(llm.Tool.parameters, vol.Schema)
    else importlib.import_module("probatio")
)


def _parameters(contract):
    properties = {}
    required = contract["inputSchema"].get("required", [])
    for key, spec in contract["inputSchema"]["properties"].items():
        marker = PARAMETER_SCHEMA.Required(key) if key in required else PARAMETER_SCHEMA.Optional(key)
        typ = spec.get("type")
        properties[marker] = {
            "string": str,
            "integer": int,
            "number": PARAMETER_SCHEMA.Any(int, float),
            "boolean": bool,
            "array": list,
            "object": dict,
        }.get(typ, object)
    return PARAMETER_SCHEMA.Schema(properties)


async def _require_admin(hass, context):
    user_id = context.context.user_id if context.context else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_admin or not user.is_active:
        raise HomeAssistantError("HA Control requires an authenticated administrator context.")


class BridgeTool(llm.Tool):
    integration = DOMAIN

    def __init__(self, entry, contract):
        self.entry = entry
        self.name = contract["name"]
        self.description = contract["description"]
        self.parameters = _parameters(contract)

    async def async_call(self, hass, tool_input, llm_context):
        await _require_admin(hass, llm_context)
        session = async_get_clientsession(hass)
        try:
            async with session.post(self.entry.data["addon_url"].rstrip("/") + "/bridge/call",
                headers={"Authorization": "Bearer " + self.entry.data["bridge_token"]},
                json={"name": self.name, "arguments": tool_input.tool_args},
                timeout=aiohttp.ClientTimeout(total=150), allow_redirects=False) as response:
                if response.status != 200:
                    raise HomeAssistantError(f"HA Control bridge returned HTTP {response.status}.")
                data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise HomeAssistantError("HA Control bridge unavailable; outcome may be uncertain. Inspect state before repeating a mutation.") from None
        if not data.get("ok"):
            error = data.get("error", {})
            raise HomeAssistantError(json.dumps(error, ensure_ascii=False))
        return llm.ToolResult(data=data) if hasattr(llm, "ToolResult") else data


class ControlAPI(llm.API):
    def __init__(self, hass, entry):
        api_args = {"hass": hass, "id": DOMAIN, "name": "HA Control (administration)"}
        if "requires_admin" in llm.API.__dataclass_fields__:
            api_args["requires_admin"] = True
        super().__init__(**api_args)
        self.entry = entry
        self.tools = [BridgeTool(entry, contract) for contract in CONTRACTS]

    async def async_get_api_instance(self, llm_context):
        await _require_admin(self.hass, llm_context)
        return llm.APIInstance(api=self, llm_context=llm_context, tools=self.tools,
            api_prompt="Control the connected Home Assistant instance through five generic tools. Discover operation schemas before requests. Use result handles for large outputs. Never repeat uncertain mutations automatically.")


async def async_setup(hass: HomeAssistant, config):
    websocket_api.async_register_command(hass, catalog_command)
    return True


async def async_setup_entry(hass: HomeAssistant, entry):
    unregister = llm.async_register_api(hass, ControlAPI(hass, entry))
    entry.async_on_unload(unregister)
    return True


async def async_unload_entry(hass: HomeAssistant, entry):
    return True
