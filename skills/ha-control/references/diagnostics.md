# Diagnostics

Start with `ha_discover()` for availability and version. A Core outage does not imply the MCP add-on or Supervisor is unavailable.

Use `ha_discover(scope="supervisor", query="...")` and `ha_request(transport="supervisor", operation=...)` for system information, resource statistics, logs, apps, backups and lifecycle operations. Supervisor response bodies use their own `result`/`data` fields. Long-running operations may return job IDs; inspect `/jobs/info` instead of resubmitting them.

Read a bounded log snapshot with `ha_request(transport="supervisor", operation="/core/logs", params={"lines":100,"no_colors":""})`; `/supervisor/logs` and `/addons/{slug}/logs` are also available. Log responses are text and use the same output limits, redaction and `ha_result` paging. `content_type` describes a request body, not the expected response. Avoid `/follow` routes in this finite request tool. HA Control app versions before 0.2.1 sent a JSON-only Accept header and could receive HTTP 400 for Core/journal logs; update the companion app if that exact error appears. It does not indicate a Core crash.

Core diagnostics are generic WebSocket/REST operations. Search live commands for `trace`, `repairs`, `system_health`, `diagnostics` or `config_entries`. Retrieve the selected command's schema before using it. Entity state alone does not identify a failing integration; relate the entity to its registry device/config entries, then inspect the relevant diagnostics and logs.

## Observe state changes

```json
{"action":"subscribe","event_type":"state_changed","entity_ids":["input_number.actual_helper"]}
```

Keep the returned `subscription_id`. Poll with `action="poll"`, `subscription_id`, and the previous `cursor`. Finish with `action="unsubscribe"`.

For a native subscription, discover its schema and pass its command as `operation`, fields as `params`. Do not pass connection `id` or override `type`. Up to 32 subscriptions exist simultaneously; each queue holds 256 records, and idle subscriptions expire after 15 minutes.

`kind="gap"` marks a connection break; `dropped_before_cursor` marks queue overflow. These are incomplete event streams, not evidence of no change. Recover from current state and Recorder history.

On `uncertain=true`, read the resource/configuration or Supervisor job state before retrying. A timed-out HTTP POST or socket disconnect can occur after a successful change. Batches report `not_executed` for operations skipped after such an outcome.

For Core recovery use Supervisor `/core/info`, `/core/logs`, `/core/check`, and POST `/core/start` or `/core/restart` as appropriate. Avoid restarting unrelated apps to diagnose one failure. MCP's own `/health` indicates its process health, not Core readiness.
