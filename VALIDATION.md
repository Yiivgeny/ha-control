# Validation

The original local deployment was checked on Home Assistant Core 2026.8.1 and 2026.9.1, Supervisor 2026.08.0, HA OS 18.2, amd64. Test counts and hardware results describe completed checks, not a promise that every integration-specific operation has been exercised.

| Agent scenario | Generic operation | Verified result |
|---|---|---|
| Capability/entity/service lookup | `ha_discover`, `get_states`, `get_services` | Live schemas and paginated inventory |
| Entity/device/area joins | `config/*_registry/list`, `ha_result` | Hundreds of registry entries joined with devices and areas |
| Verified control | Helper CRUD, `call_service`, state read | Temporary numeric helper reached requested value and was removed |
| Event observation | `ha_events` | Real state-change event; unsubscribe cleanup |
| Recorder history | REST history with explicit bounds | Nonempty history |
| Statistics | Recorder WebSocket commands | Non-null aggregate series |
| InfluxQL discovery/analytics | HTTP profile `/query` | Database, measurement, field/tag discovery and nonempty aggregation |
| Automation configuration | Generic REST config routes | Create, update, read back and delete temporary automation |
| Dashboard configuration | Lovelace WebSocket routes | Create/update/read/delete with validated live entity references |
| Configuration files | `ha_files` | Diff, hash conflict, patch, atomic replacement and restore |
| Core outage | Supervisor and file tools | Remained available; guarded temporary `.storage` recovery |
| Core recovery | Core start and subscriptions | Reconnected with explicit gap; native LLM API available |
| Native LLM API | Same five tools through bridge | Real HTTP roundtrip; non-admin denied |
| Hermes | Real MCP calls from the client | Inventory and HTTP analytics through the MCP tools |

The unit suite covers the 8 KiB contract budget, nested pagination/expiry, redaction, file guards/revisions, separate authentication, generated identity persistence/permissions, Supervisor routes/discovery retry, batch validation, uncertain outcomes without replay, redirects, early events, overflow and reconnect gaps.

Measured compact SDK `tools/list`: **five tools, 4352 bytes**. A runtime RSS observation was about 67.5 MiB; this is not a load benchmark. File/source hashes were compared with the deployed containers. Private evidence and installation credentials are deliberately excluded from Git.

Config/options flows, script/scene changes, traces and every administrative API were not all individually mutated during acceptance. Dashboard checks validate configuration/readback and references, not every visual card rendering. No physical devices were actuated by acceptance tests.

CI runs the unit suite and builds the HACS archive before publishing a versioned app image and GitHub release. See the repository Actions and Releases for the status of the published version.
