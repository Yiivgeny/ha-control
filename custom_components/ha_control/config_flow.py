"""Pair with the companion through authenticated Supervisor discovery."""

import asyncio

from aiohasupervisor import SupervisorError
import aiohttp
import voluptuous as vol

from homeassistant.components.hassio.handler import get_supervisor_client
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

# Supervisor hashes the canonical repository URL (lowercase SHA1 prefix).
# Keep the original local companion accepted for migration/development.
ADDON_URLS = {
    "369902b3_ha_control_mcp": "http://369902b3-ha-control-mcp:8213",
    "local_ha_control_mcp": "http://local-ha-control-mcp:8213",
}


class HAControlConfigFlow(ConfigFlow, domain="ha_control"):
    VERSION = 1

    async def _connect(self, config, slug):
        # Core fetches this payload from authenticated Supervisor discovery.
        # Reject other apps claiming our service, and arbitrary callback URLs.
        addon_url = ADDON_URLS.get(slug)
        if addon_url is None or config.get("addon_url") != addon_url:
            return self.async_abort(reason="invalid_server")
        token = config.get("bridge_token", "")
        if not isinstance(token, str) or len(token) < 32:
            return self.async_abort(reason="invalid_server")
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(addon_url + "/bridge/tools",
                headers={"Authorization": "Bearer " + token},
                timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False) as response:
                if response.status != 200 or len((await response.json()).get("tools", [])) != 5:
                    return self.async_abort(reason="cannot_connect")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            return self.async_abort(reason="cannot_connect")
        data = {"addon_url": addon_url, "bridge_token": token, "addon_slug": slug}
        await self.async_set_unique_id("ha_control")
        # Updates legacy entries and identities changed by an app restore.
        # BridgeTool reads current entry.data on every call.
        self._abort_if_unique_id_configured(updates=data, reload_on_update=False)
        return self.async_create_entry(title="HA Control", data=data)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo):
        return await self._connect(discovery_info.config, discovery_info.slug)

    async def async_step_user(self, user_input=None):
        try:
            discoveries = await get_supervisor_client(self.hass).discovery.list()
            for slug in ADDON_URLS:
                for item in discoveries:
                    if item.service == "ha_control" and item.addon == slug:
                        return await self._connect(item.config, item.addon)
        except (SupervisorError, KeyError):
            pass
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}),
            errors={"base": "addon_not_ready"} if user_input is not None else {})
