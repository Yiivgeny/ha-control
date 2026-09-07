# History and analytics

Use the instance timezone returned by discovery (examples below use `Europe/Moscow`). Use explicit start/end instants with UTC offsets or UTC `Z`. Choose Recorder for recent entity transitions, recorder statistics for supported aggregates, and InfluxDB for longer windows and InfluxQL analysis.

## Recorder

Discover REST `history` for `/api/history/period/{start}`. Supply `filter_entity_id` and `end_time`; use minimal responses and omit attributes when not needed. History may include an initial state at the start boundary, not an actual change at that timestamp.

Find native statistics commands with `ha_discover(scope="websocket", query="statistics")`; discover IDs using `recorder/list_statistic_ids`. Inspect the live schema for periods, types and timestamps. Do not assume every entity has long-term statistics or that numeric state strings have compatible units.

## InfluxQL through a generic HTTP profile

Discover `scope="profiles"`; the configured profile is `influx`, with the current default database exposed as metadata. Credentials are injected by the server. Do not infer which Home Assistant instance produced the data from the profile name.

```json
{"transport":"http","profile":"analytics","operation":"/query","params":{"q":"SHOW DATABASES"}}
```

Use the same endpoint for discovery:

- `SHOW MEASUREMENTS LIMIT 50`
- `SHOW FIELD KEYS FROM "<measurement>"`
- `SHOW TAG KEYS FROM "<measurement>"`
- `SHOW TAG VALUES FROM "<measurement>" WITH KEY = "entity_id" LIMIT 50`

Include `db:"<discovered-database>"` where required. Inspect `results[].error` even when HTTP succeeds. Follow `ha_result` pointers into series/values for large results.

Do not assume a measurement equals the entity's domain, that the stored entity_id includes the domain, or that the numeric field is called `value`. Discover the actual measurement, tags and fields first.

For time series, select explicit fields, constrain both time bounds and relevant tags, and aggregate to a suitable interval. Example shape after discovering real names:

```sql
SELECT mean("numeric_field") FROM "actual_measurement"
WHERE time >= '2026-09-01T00:00:00+03:00'
  AND time < '2026-09-02T00:00:00+03:00'
  AND "entity_id"='actual_tag_value'
GROUP BY time(15m) fill(null) tz('Europe/Moscow')
```

InfluxQL timezone belongs in the query's `tz(...)` clause for SELECT queries; an arbitrary HTTP `tz` parameter is not a substitute. Use a bounded existence probe when checking freshness. Avoid whole-database scans and `SELECT *` over long periods. Preserve nulls and unit differences when reporting aggregates.
