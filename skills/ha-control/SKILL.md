---
name: ha-control
description: Operate and administer a connected Home Assistant instance through five generic HA Control MCP tools, including configuration, dashboards, diagnostics and historical analytics.
---

# HA Control

Read `ha_discover()` to identify the connected instance and its timezone. Client-specific addresses, credentials and multi-home routing belong in private client configuration.

Use the connected MCP tools whose names end in `ha_discover`, `ha_request`, `ha_files`, `ha_events`, `ha_result`. The server obtains HA authorization automatically from Supervisor; its internal Core bridge pairs through Supervisor Discovery. Only the external MCP token and HTTP-profile credentials are configured. Do not send HA/Influx tokens in tool arguments. No SSH or shell is needed for normal operation.

If multiple homes are connected, follow the client's private routing instructions. Resolve an ambiguous target before making changes and never reuse another home's identifiers.

## Working pattern

1. Discover narrowly: `ha_discover(scope="entities", query="...")`, `scope="services"`, or `scope="websocket"`. Use `operation` to load the exact schema. The live HA schema is authoritative; fallback schemas explicitly identify unsupported validators.
2. Read current state/configuration and use actual identifiers. A config entry ID, automation config ID, entity ID and dashboard URL path are different identifiers.
3. Execute with `ha_request`: `websocket` takes a command in `operation` and its fields in `params`; `rest` and `supervisor` take an origin-relative path, HTTP method, optional query `params` and `body`. `http` additionally selects a saved `profile`.
4. Verify the outcome. A successful service call is not proof that a physical device reached the requested state. Read or subscribe to state changes. Never use a state-machine POST as a substitute for physical control.

Large responses provide `result_id`. Follow `next_offset` and `nested_pointers` using `ha_result`; `pointer` is RFC6901, e.g. `/data` or `/0/states`. `fields` selects object keys. Stored results expire after 15 minutes and are never recreated by repeating a mutation. Discovery pagination uses `ha_discover(offset=...)`, distinct from stored-result pagination.

`ha_request(requests=[...])` executes at most 20 requests sequentially, with per-item outcomes. It is not atomic. Inspect every result; an uncertain result stops the remaining items. If an error says `uncertain=true`, inspect actual state before deciding whether another call is needed.

For event observations, subscribe **before** changing state. Poll by the last returned cursor and unsubscribe when finished. Disconnect gaps and queue overflow mean the event record is incomplete; query current state/history to recover context.

## Task references

- [Configuration](references/configuration.md): registries, helpers, config flows, automation/script/scene editing, files and recovery.
- [Diagnostics](references/diagnostics.md): Supervisor, logs, traces, subscriptions and Core outages.
- [Dashboards](references/dashboards.md): join live state/registries, room placement, valid Lovelace configuration and export.
- [Analytics](references/analytics.md): Recorder/statistics and InfluxQL discovery/aggregation through generic HTTP requests.

Operate within the user's authorized task. Existing authorization carries through its necessary steps; do not insert blanket confirmation requirements for ordinary configuration work. Show material changes and verification results. Use APIs for managed configuration, and previews/hashes for file edits.
