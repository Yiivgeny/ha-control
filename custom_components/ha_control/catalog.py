"""Runtime WS catalog. Registry introspection is isolated here for version testing."""

import inspect

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.helpers.llm import async_get_apis


def _fallback(schema, path="", unsupported=None):
    """Document unknown validators without silently pretending they accept anything."""
    unsupported = unsupported if unsupported is not None else []
    if isinstance(schema, vol.Schema):
        return _fallback(schema.schema, path, unsupported)
    if isinstance(schema, dict):
        properties, required = {}, []
        for key, value in schema.items():
            name = key.schema if isinstance(key, vol.Marker) else key
            if not isinstance(name, str):
                unsupported.append({"path": path, "validator": "dynamic_mapping_key"})
                continue
            properties[name] = _fallback(value, path + "/" + name, unsupported)
            if isinstance(key, vol.Required):
                required.append(name)
        return {"type": "object", "properties": properties, "required": required}
    if isinstance(schema, list):
        return {"type": "array", "items": _fallback(schema[0], path + "/*", unsupported) if len(schema) == 1 else {}}
    for typ, json_type in [(str, "string"), (bool, "boolean"), (int, "integer"), (float, "number"), (dict, "object"), (list, "array")]:
        if schema is typ:
            return {"type": json_type}
    if isinstance(schema, (str, int, float, bool)) or schema is None:
        return {"const": schema}
    if isinstance(schema, vol.Any):
        return {"anyOf": [_fallback(v, path, unsupported) for v in schema.validators]}
    if isinstance(schema, vol.All):
        return {"allOf": [_fallback(v, path, unsupported) for v in schema.validators]}
    if isinstance(schema, vol.In) and isinstance(schema.container, (list, tuple, set, dict)):
        return {"enum": list(schema.container)}
    if isinstance(schema, vol.Coerce):
        return _fallback(schema.type, path, unsupported)
    if isinstance(schema, vol.Range):
        return {k: v for k, v in {"minimum": schema.min, "maximum": schema.max}.items() if v is not None}
    if isinstance(schema, vol.Length):
        unsupported.append({"path": path, "validator": "Length", "min": schema.min, "max": schema.max})
        return {"x-ha-validator": "Length"}
    name = getattr(schema, "__qualname__", type(schema).__qualname__)
    unsupported.append({"path": path, "validator": name})
    return {"x-ha-validator": name}


def describe_schema(schema):
    # Avoid incidental packages from Core's Python environment. Home Assistant
    # 2026.9 removed voluptuous_openapi, which was present in 2026.8 but was not
    # an integration API. Unknown validators remain explicit and HA validates
    # the original schema when the command is executed.
    unsupported = []
    converted = _fallback(schema, unsupported=unsupported)
    return {"schema": converted, "schema_complete": not unsupported, "unsupported_validators": unsupported,
            "validation": "Descriptive schema only. Home Assistant validates the original schema at execution."}


@websocket_api.websocket_command({
    vol.Required("type"): "ha_control/catalog",
    vol.Optional("query", default=""): str,
    vol.Optional("operation"): vol.Any(str, None),
    vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0)),
    vol.Optional("limit", default=50): vol.All(int, vol.Range(min=1, max=50)),
})
@websocket_api.require_admin
@callback
def catalog_command(hass, connection, msg):
    registry = hass.data.get(websocket_api.DOMAIN)
    if not isinstance(registry, dict):
        connection.send_error(msg["id"], "unsupported_core", "WebSocket registry layout changed; update HA Control.")
        return
    operation = msg.get("operation")
    if operation:
        item = registry.get(operation)
        if not isinstance(item, tuple) or len(item) != 2:
            connection.send_error(msg["id"], "not_found", "Command is not registered.")
            return
        handler, schema = item
        description = describe_schema(schema)
        properties = description["schema"].get("properties", {})
        for managed in ["id", "type"]:
            properties.pop(managed, None)
        description["schema"]["required"] = [k for k in description["schema"].get("required", []) if k not in {"id", "type"}]
        data = {"operation": operation, "transport": "websocket", "description": inspect.getdoc(handler) or "",
                "managed_wire_fields": ["id", "type"], **description}
    else:
        query = msg["query"].casefold()
        rows = []
        for name, item in sorted(registry.items()):
            description = (inspect.getdoc(item[0]) or "").split("\n")[0]
            if query and query not in (name + " " + description).casefold():
                continue
            rows.append({"operation": name, "description": description})
        offset, limit = msg["offset"], msg["limit"]
        data = {"items": rows[offset:offset + limit], "total": len(rows),
                "llm_api_registered": any(api.id == "ha_control" for api in async_get_apis(hass)),
                "next_offset": offset + limit if offset + limit < len(rows) else None}
    connection.send_result(msg["id"], data)
