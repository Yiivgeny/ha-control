# Configuration and recovery

## Native APIs first

Find commands by intent with `ha_discover(scope="websocket", query="registry")`, `query="input_number"`, `query="config_entries"` or another relevant term. Then request `operation=<exact command>` to retrieve the live schema. There are no integration-specific MCP tools.

Typical registry reads:

```json
{"requests":[
  {"transport":"websocket","operation":"config/area_registry/list"},
  {"transport":"websocket","operation":"config/device_registry/list"},
  {"transport":"websocket","operation":"config/entity_registry/list"}
]}
```

Use discovered create/update/delete operations for helpers, areas, labels, devices and entities. Read back the specific object after changes. Do not copy objects wholesale from list responses into update calls: output fields are not necessarily accepted input fields.

Integration setup uses REST config flows. Discover `scope="rest", query="flow"`; begin with a handler domain and continue the returned `flow_id` according to each step's `data_schema`. Handle `form`, `menu`, `external`, `create_entry` and `abort` as distinct results. External login steps need the user's interaction when the provider requires it; do not invent credentials or completion state.

Automation/script/scene configs use `/api/config/{kind}/config/{id}` with GET/POST/DELETE as supported. An automation's config ID is not its entity ID. Keep the original mapping, make the requested change, validate it and verify the resulting entity/config. Use discovered reload services when the relevant API does not reload itself.

## File operations

Roots are `homeassistant`, `addons`, `addon_configs`, `share`, `media`. A path is `homeassistant/configuration.yaml`, not a host filesystem path. Symlinks and dot-segment traversal are rejected. Editing is for UTF-8 configuration files up to 8 MiB.

1. `ha_files(action="read", path=...)` returns content and the original file hash.
2. Prepare `write` with complete content or `patch` with `edits:[{old,new}]`. Every old fragment must occur exactly once. Supply `expected_hash`; use `missing` only for a new file.
3. The default is a diff preview. Apply the same operation with `apply=true` when it matches the authorized change.
4. Retain `revision_id`; `restore` with the current `expected_hash` restores the earlier content (or removes a file that did not previously exist).

Redaction may replace secrets in returned content. Never write back a redacted full document. Use an exact patch on unrelated text to preserve secret values in the original file. Move requires an absent destination and existing parent; delete applies to regular files, not recursive directory trees.

Validate HA configuration with POST `/api/config/core/check_config` or Supervisor POST `/core/check`. Check the response's own validity/error fields, not just HTTP success. Reload the affected domain where supported; restart Core only when required.

Direct `.storage` writes require an acknowledged stop through `ha_request(transport="supervisor", operation="/core/stop", method="POST", body={})`. Supervisor's info response has no running-state field, so the server reasserts that explicit stop immediately before editing. After an add-on restart, issue the stop again. Prefer managed APIs. For recovery: stop Core, read/patch/restore the affected file with hashes, then start Core and verify status. Do not change `.storage` while Core runs: cached state can overwrite the edit.

The MCP add-on remains reachable while Core is stopped. Supervisor controls backups and app lifecycle; discover the needed route rather than depending on shell commands.
