"""Live acceptance: only uniquely named temporary resources are changed and removed."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid
import sys

from mcp_client import client, call

REPORT = []


def record(name, **details):
    REPORT.append({"scenario": name, "passed": True, **details})
    print(name, "PASS", json.dumps(details, ensure_ascii=False), flush=True)


async def materialize(session, result):
    if not result.get("ok"):
        raise AssertionError(json.dumps(result, ensure_ascii=False))
    if not result.get("result_id"):
        return result["data"]
    key = result["result_id"]
    async def value_at(pointer=""):
        offset, combined = 0, None
        while True:
            page = await call(session, "ha_result", {"result_id": key, "pointer": pointer, "offset": offset})
            assert page["ok"], page
            async def expand(value):
                if isinstance(value, dict):
                    if "$pointer" in value:
                        return await value_at(value["$pointer"])
                    return {k: await expand(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [await expand(v) for v in value]
                return value
            data = await expand(page["data"])
            if combined is None:
                combined = data
            elif isinstance(data, dict):
                combined.update(data)
            else:
                combined += data
            if page["next_offset"] is None:
                return combined
            offset = page["next_offset"]
    return await value_at()


async def main():
    suffix = uuid.uuid4().hex[:8]
    helper_id = None
    dashboard_id = None
    subscription_id = None
    automation_id = "ha_control_acceptance_" + suffix
    dashboard_path = "ha-control-test-" + suffix
    file_path = "share/ha-control-test-" + suffix + ".yaml"
    saved_automation = False
    file_hash = None
    async with client() as session:
        async def invoke(name, **args):
            return await materialize(session, await call(session, name, args))
        async def ws(operation, **params):
            return await invoke("ha_request", transport="websocket", operation=operation, params=params)
        async def rest(operation, method="GET", **kwargs):
            return await invoke("ha_request", transport="rest", operation=operation, method=method, **kwargs)
        try:
            tools = await session.list_tools()
            size = len(json.dumps(tools.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False, separators=(",", ":")).encode())
            assert len(tools.tools) == 5 and size <= 8192
            record("compact_tools", tools=5, bytes=size)
            overview = await invoke("ha_discover")
            assert isinstance(overview["core"]["version"], str) and overview["core"]["version"]
            record("overview", version=overview["core"]["version"])
            catalog = await invoke("ha_discover", scope="websocket", query="registry")
            assert catalog["total"] > 0
            schema = await invoke("ha_discover", scope="websocket", operation="recorder/statistics_during_period")
            assert "schema_complete" in schema
            record("live_schemas", registry_commands=catalog["total"], fallback_explicit=not schema["schema_complete"])
            states = await ws("get_states")
            registry = await ws("config/entity_registry/list")
            devices = await ws("config/device_registry/list")
            areas = await ws("config/area_registry/list")
            assert len(registry) > 50
            device_by_id = {d["id"]: d for d in devices}
            joined = {e["entity_id"]: e.get("area_id") or device_by_id.get(e.get("device_id"), {}).get("area_id") for e in registry}
            record("inventory_and_pagination", states=len(states), registry=len(registry), devices=len(devices), areas=len(areas), assigned_entities=sum(bool(a) for a in joined.values()))
            sensor = next(s["entity_id"] for s in states if s["entity_id"].startswith("sensor.") and s["attributes"].get("unit_of_measurement") == "°C" and s["state"] not in {"unavailable", "unknown"})
            now = datetime.now(timezone.utc)
            history = await rest("/api/history/period/" + (now - timedelta(hours=6)).isoformat(), params={"filter_entity_id": sensor, "end_time": now.isoformat(), "minimal_response": "", "no_attributes": ""})
            assert isinstance(history, list)
            stat_ids = await ws("recorder/list_statistic_ids")
            statistics = [s["statistic_id"] for s in stat_ids if s.get("has_mean")][:30]
            stats = await ws("recorder/statistics_during_period", start_time=(now - timedelta(days=7)).isoformat(), end_time=now.isoformat(), statistic_ids=statistics, period="day", types=["mean"])
            assert stats and any(row.get("mean") is not None for rows in stats.values() for row in rows)
            record("recorder_and_statistics", history_groups=len(history), statistic_ids=len(stat_ids), returned_statistics=len(stats))
            async def influx(query, db=True):
                params = {"q": query}
                if db:
                    params["db"] = "homeassistant"
                data = await invoke("ha_request", transport="http", profile="influx", operation="/query", params=params)
                assert all("error" not in r for r in data["results"]), data
                return data
            await influx("SHOW DATABASES", False)
            await influx("SHOW MEASUREMENTS LIMIT 3")
            fields = await influx('SHOW FIELD KEYS FROM "sensor"')
            await influx('SHOW TAG KEYS FROM "sensor"')
            tags = await influx('SHOW TAG VALUES FROM "sensor" WITH KEY = "entity_id" LIMIT 3')
            numeric_fields = [v[0] for v in fields["results"][0]["series"][0]["values"] if v[1] in {"float", "integer"}]
            numeric = "value" if "value" in numeric_fields else numeric_fields[0]
            probe_field = numeric.replace('"', '\\"')
            recent = await influx(f'SELECT last("{probe_field}") FROM "sensor" WHERE time > now() - 24h GROUP BY "entity_id" LIMIT 1')
            tag = next(series["tags"]["entity_id"] for series in recent["results"][0]["series"] if series["values"][0][1] is not None)
            escaped_tag = tag.replace("\\", "\\\\").replace("'", "\\'")
            escaped_field = numeric.replace('"', '\\"')
            aggregate = await influx(f'''SELECT mean("{escaped_field}") FROM "sensor" WHERE time >= '{(now-timedelta(days=1)).isoformat()}' AND time < '{now.isoformat()}' AND "entity_id"='{escaped_tag}' GROUP BY time(1h) fill(null) tz('Europe/Moscow')''')
            assert any(v[1] is not None for series in aggregate["results"][0].get("series", []) for v in series["values"])
            record("influx_discovery_and_aggregation", measurement="sensor", field=numeric, series=len(aggregate["results"][0].get("series", [])))
            if "--read-only" in sys.argv:
                return
            helper = await ws("input_number/create", name="HA Control Acceptance " + suffix, min=0, max=100, initial=0, mode="box")
            helper_id = helper["id"]
            entity = "input_number." + helper_id
            await ws("input_number/update", input_number_id=helper_id, name="HA Control Acceptance " + suffix, min=0, max=200, mode="box")
            sub = await invoke("ha_events", action="subscribe", entity_ids=[entity])
            subscription_id = sub["subscription_id"]
            await ws("call_service", domain="input_number", service="set_value", target={"entity_id": entity}, service_data={"value": 42})
            state = await rest("/api/states/" + entity)
            assert float(state["state"]) == 42
            events = await invoke("ha_events", action="poll", subscription_id=subscription_id)
            assert events["events"]
            record("helper_crud_service_and_events", state=state["state"], events=len(events["events"]))
            automation = {"id": automation_id, "alias": "HA Control Acceptance " + suffix, "triggers": [{"trigger": "event", "event_type": automation_id}], "conditions": [], "actions": [{"variables": {"validated": True}}], "mode": "single"}
            await rest("/api/config/automation/config/" + automation_id, "POST", body=automation)
            saved_automation = True
            read = await rest("/api/config/automation/config/" + automation_id)
            assert read["alias"] == automation["alias"]
            automation["alias"] += " Updated"
            await rest("/api/config/automation/config/" + automation_id, "POST", body=automation)
            read = await rest("/api/config/automation/config/" + automation_id)
            assert read["alias"] == automation["alias"]
            record("automation_create_update_read")
            dashboard = await ws("lovelace/dashboards/create", title="HA Control Acceptance", url_path=dashboard_path, mode="storage", show_in_sidebar=False, require_admin=True)
            dashboard_id = dashboard["id"]
            config = {"views": [{"type": "sections", "path": "test", "title": "Acceptance", "sections": [{"type": "grid", "cards": [{"type": "tile", "entity": entity}, {"type": "tile", "entity": sensor}]}]}]}
            live_ids = {s["entity_id"] for s in await ws("get_states")}
            refs = [c["entity"] for c in config["views"][0]["sections"][0]["cards"]]
            assert all(ref in live_ids for ref in refs)
            await ws("lovelace/config/save", url_path=dashboard_path, config=config)
            assert await ws("lovelace/config", url_path=dashboard_path) == config
            config["views"][0]["title"] = "Acceptance Updated"
            await ws("lovelace/config/save", url_path=dashboard_path, config=config)
            assert await ws("lovelace/config", url_path=dashboard_path) == config
            record("dashboard_create_validate_update_read", entity_references=len(refs))
            preview = await invoke("ha_files", action="write", path=file_path, content="value: 1\n", expected_hash="missing")
            assert not preview["applied"]
            created = await invoke("ha_files", action="write", path=file_path, content="value: 1\n", expected_hash="missing", apply=True)
            file_hash = created["after_hash"]
            conflict = await call(session, "ha_files", {"action": "write", "path": file_path, "content": "invalid\n", "expected_hash": "missing", "apply": True})
            assert not conflict["ok"] and conflict["error"]["code"] == "conflict"
            changed = await invoke("ha_files", action="patch", path=file_path, edits=[{"old": "value: 1", "new": "value: 2"}], expected_hash=file_hash, apply=True)
            file_hash = changed["after_hash"]
            restored = await invoke("ha_files", action="restore", path=file_path, revision_id=changed["revision_id"], expected_hash=file_hash, apply=True)
            file_hash = restored["after_hash"]
            assert (await invoke("ha_files", action="read", path=file_path))["content"] == "value: 1\n"
            forbidden = await call(session, "ha_files", {"action": "write", "path": "homeassistant/.storage/ha_control_acceptance_" + suffix, "content": "{}", "expected_hash": "missing", "apply": True})
            assert not forbidden["ok"] and forbidden["error"]["code"] == "core_running"
            record("file_preview_conflict_restore_and_storage_guard")
        finally:
            if subscription_id:
                await invoke("ha_events", action="unsubscribe", subscription_id=subscription_id)
            if file_hash:
                await invoke("ha_files", action="delete", path=file_path, expected_hash=file_hash, apply=True)
            if dashboard_id:
                await ws("lovelace/dashboards/delete", dashboard_id=dashboard_id)
            if saved_automation:
                await rest("/api/config/automation/config/" + automation_id, "DELETE")
            if helper_id:
                await ws("input_number/delete", input_number_id=helper_id)
            states = await ws("get_states")
            assert not any(suffix in s["entity_id"] for s in states)
            record("temporary_resources_removed", suffix=suffix)
            output = ".local/acceptance-readonly.json" if "--read-only" in sys.argv else ".local/acceptance.json"
            Path(output).write_text(json.dumps(REPORT, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
