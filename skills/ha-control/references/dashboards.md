# Dashboards from live HA data

Collect current states (`websocket/get_states`) plus entity, device and area registries. Follow result pagination. Build a map by entity ID and device ID; use explicit entity `area_id` first, device `area_id` second. Treat `suggested_area` as a suggestion, not a confirmed registry area. Keep unassigned system entities separate from rooms.

Design from the current instance entities and the user's requested purpose. Start with built-in cards: sections views, grid, heading, tile, gauge, entity-filter, history-graph and entities. Add custom cards only when requested or already explicitly required by the existing dashboard.

Search `ha_discover(scope="websocket", query="lovelace")`. Inspect exact schemas for listing/creating dashboards and loading/saving configuration. A dashboard ID and its `url_path` are different; use the identifier required by each command. When updating, load the current configuration and preserve unrelated views/cards.

Before saving:

- Parse the complete JSON/YAML as a mapping and validate every actual entity reference against live states or the entity registry, according to the card's intended purpose.
- Traverse nested cards, sections, conditions and entity lists; do not rely on one regex that misses nested fields.
- Distinguish explicitly configured but currently unavailable entities from nonexistent identifiers; do not invent replacements.
- Serialize exported YAML without aliases/anchors. Use native Lovelace JSON for API saves.

Save, read the dashboard back and compare the requested structure. For delivery, export YAML through `ha_files` into `share/<name>.yaml` or generate a local artifact from the verified configuration; in Hermes Telegram delivery use its normal local attachment workflow. Do not treat a remote HA path as an existing local attachment.

Room composition, safety thresholds and entity names must come from the actual instance inventory and user intent. A dashboard from a different home is not a template for entity identifiers.
