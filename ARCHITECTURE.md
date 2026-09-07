# Architecture and design decisions

## One engine, two entry points

The companion app owns execution, transport adapters, files, subscriptions and result storage. MCP and the native HA LLM API invoke the same `Engine.invoke` implementation. The integration has no MCP dependency: it registers `llm.API` and exposes live Core metadata through an admin-only WebSocket command.

The app reads metadata from Core; the integration delegates execution to the app. Supervisor and mounted files remain usable during a Core outage. The public interface deliberately excludes shell, container exec and arbitrary Python execution.

## Discovery instead of thousands of tools

Five static contracts are stored in `addon/ha_control/contracts.json`; release tooling copies the same file into the integration. Entity/service/integration identifiers are arguments. `ha_discover` returns small search results and only expands a selected operation's schema.

The integration inspects registered WebSocket commands in the installed Core. Managed wire fields `id` and `type` are excluded from user arguments. Schema conversion explicitly marks unsupported validators and incomplete output; HA remains the final validator. Service descriptions/selectors come from HA. A small versioned REST/Supervisor catalog is advisory and does not restrict direct relative paths.

## Authentication and internal networking

The companion uses `SUPERVISOR_TOKEN` with `http://supervisor/core/api/`, `ws://supervisor/core/websocket`, and `http://supervisor/`. It does not store a separate HA long-lived token or route internal Core traffic through the host's LAN address.

The external MCP token is independent of the internal bridge identity. The latter is created once in `/data/bridge_token` with mode 0600 and is not an app option. Supervisor Discovery carries it to Core; that API only lets the app declare services listed in its manifest, and Core fetches discovery using its own Supervisor credentials.

The integration accepts the canonical repository app's exact slug and hostname, plus the original local companion for migration. Single-instance behavior uses `unique_id`; the manifest's `single_config_entry` flag would block discovery refresh before the integration can update credentials. A changed identity updates the existing config entry, and the bridge reads current entry data for every call.

HTTP profiles pin the origin and inject their own authentication. Absolute request URLs, traversal and redirects are rejected. Redaction covers known credentials, secret field names and common textual secret forms before output or result storage. Audit logs contain only tool name, outcome, time and duration, with rotation near 2 MiB. Redaction is not a substitute for treating all client access as administrative.

## Bounded state and uncertain operations

Output defaults to 50 items and 16 KiB. Results are stored in a 64 MiB memory budget for 15 minutes; JSON Pointer and field selection allow retrieval of nested data. Individual upstream HTTP responses are capped at 32 MiB.

Batches run sequentially, up to 20 items with a shared 120-second budget. They are not transactions. An interrupted mutation is marked uncertain; remaining dependent items are skipped. Reconnection never replays pending user commands. Discovery publication retries separately because Supervisor de-duplicates the same declaration.

One WebSocket carries requests and at most 32 subscriptions. Each queue retains 256 records and expires after 15 idle minutes. A reconnect inserts `gap`; overflow exposes missing cursors. Consumers recover context through state reads and history.

## Files and recovery

Allowed roots are HA configuration, local apps, app configurations, share and media. Paths with traversal or symlinks are rejected. UTF-8 files are limited to 8 MiB. Changes require a source hash, preview by default, use atomic replacement and persist revisions. Exact patches preserve secrets hidden in a returned document; writing a redacted full document is rejected.

A `.storage` edit requires a successful Core stop through this engine. Before editing, the engine reasserts the stop using Supervisor; an unreachable Core is not sufficient evidence. A lock serializes file operations with local Core lifecycle requests. Independent external administrators can still interfere and must coordinate recovery work.

Revisions persist in `/data/revisions` with no automatic pruning. Restore recovers the old content of one path. After a move, restoring the original source does not delete the destination; a complete inverse move requires separately checking/removing it.

## Generic scope

States, services, registries, helpers, config/options flows, automation/script/scene configuration, Lovelace, Recorder, statistics, traces, diagnostics and Supervisor are available through generic requests. Interactive flows follow returned `flow_id` and step schemas; provider authentication may require user interaction.

The skill covers inventory joins, verified control, Recorder/statistics, InfluxQL schema discovery and analytics, and dashboards built from live entities with built-in cards and YAML without anchors. Site routing and credentials belong in the installed client's private configuration, not in this repository.
