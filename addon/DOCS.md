# HA Control

Five generic MCP tools for deep Home Assistant administration, plus a native Home Assistant LLM API and a reusable agent skill. Original Python code; official MCP SDK **2.0.0** runs only in the companion app.

| Tool | Purpose |
|---|---|
| `ha_discover` | Search live capabilities; load one operation's schema |
| `ha_request` | REST, WebSocket, Supervisor and configured HTTP profiles; sequential batches |
| `ha_files` | Read, preview, hash-check, patch, replace, move, delete and restore configuration |
| `ha_events` | Bounded event subscriptions with explicit connection gaps |
| `ha_result` | Page stored results and select fields/JSON pointers |

The compact `tools/list` response is **4352 bytes**, independent of installed integrations. Service names, entities, devices, config flows and dashboard identifiers are data, not separate tools.

## Install the companion app

Requires Home Assistant OS or Supervised, Core 2026.8.1 or newer, on **amd64 or aarch64**. Core 2026.9.4, the latest stable release at publication time, was checked on a live amd64 instance. Both images were built and smoke-tested locally, including a native ARM64 Docker build. A Home Assistant OS installation on ARM has not yet been tested.

1. Open **Settings → Apps → App store → Repositories** (called Add-ons on older versions).
2. Add `https://github.com/Yiivgeny/ha-control`.
3. Install **HA Control MCP**. Supervisor downloads the prebuilt image from `ghcr.io/yiivgeny/ha-control-mcp`; no local build or SSH is needed.
4. Set `mcp_token` to a random secret of at least 32 characters and configure any optional HTTP profiles. Start the app and enable startup/watchdog as desired.

Only the external MCP token and optional HTTP-profile credentials are user settings. The app obtains HA/Supervisor authorization from `SUPERVISOR_TOKEN` automatically. Internal REST and WebSocket traffic uses Supervisor proxies. A private bridge identity is generated in the app's data directory and passed to Core through authenticated Supervisor Discovery.

## Install the integration through HACS

1. In **HACS → Custom repositories**, add `https://github.com/Yiivgeny/ha-control` with category **Integration**.
2. Download **HA Control** and restart Home Assistant.
3. The running companion is paired automatically. If needed, select **Settings → Devices & services → Add integration → HA Control**; there are no address/token fields to fill in.

The native LLM API is **HA Control (administration)**, ID `ha_control`. Select it in a compatible conversation integration. It requires an authenticated administrator context; contexts without a user ID cannot use administrative tools.

This is a HACS custom repository, not a claim of inclusion in HACS's default catalog.

## Connect Hermes or another MCP client

The endpoint is `http://<your-ha-host>:8213/mcp`, using Streamable HTTP and an `Authorization: Bearer ...` header. Never expose the Supervisor token to external clients.

Hermes configuration example:

```yaml
mcp_servers:
  ha_control:
    url: http://<your-ha-host>:8213/mcp
    headers:
      Authorization: "Bearer ${HA_CONTROL_MCP_TOKEN}"
    timeout: 150
    connect_timeout: 30
```

Store `HA_CONTROL_MCP_TOKEN` in Hermes's private environment file. Copy `skills/ha-control` into the client's skill directory. For multiple homes, add local routing instructions to the installed skill; discover actual entities and profiles instead of copying identifiers from another home.

## HTTP profiles

Profiles fix the upstream origin and inject credentials. For an app-hosted InfluxDB, use its Supervisor internal hostname, such as `http://<repository-id>-influxdb:8086`. Discover the actual hostname in Supervisor; the repository ID depends on the InfluxDB app source.

```yaml
http_profiles:
  - name: analytics
    url: http://<influx-app-hostname>:8086
    username: <database-user>
    password: <database-password>
    database: <database-name>
    description: InfluxDB historical analytics
```

Then use `ha_request` with transport `http`, that profile, path `/query` and InfluxQL parameters. Profiles are generic; no Influx-specific MCP tool is registered. Database credentials remain separate from HA authorization.

## Behavior and boundaries

Responses default to at most 50 items and 16 KiB; larger results have a 15-minute handle. Batches contain at most 20 requests and are not transactions. Uncertain mutation outcomes are never automatically repeated. Events report overflow and reconnect gaps.

Prefer native APIs for managed configuration. File edits default to a diff, require the original SHA256, and retain previous revisions. Direct `.storage` edits require an acknowledged Core stop through this app. Supervisor and mounted files remain reachable while Core is stopped. Backups use Supervisor APIs.

This is an administrative service for trusted clients. Mounted configuration and app management grant broad control. There is no public shell, Docker exec or Python execution endpoint. The default local HTTP endpoint does not encrypt traffic; use TLS termination for untrusted networks.

## Development and releases

```sh
python -m pip install -r addon/requirements.lock
PYTHONPATH=addon python -m unittest discover -s tests -q
python scripts/release.py
```

`addon/Dockerfile` downloads locked wheels for each target architecture in a build stage and installs them offline into the runtime image. A `v<version>` Git tag runs the tests, builds and smoke-tests both GHCR architectures, publishes a multi-architecture image at `ghcr.io/yiivgeny/ha-control-mcp:<version>`, and attaches `ha_control.zip` to a GitHub release for HACS. Individual `<version>-amd64` and `<version>-aarch64` image tags are also published. Version tags must match the integration and app manifests. Workflows pin actions to commit SHAs.

[Architecture](ARCHITECTURE.md) · [Validation](VALIDATION.md) · [Agent skill](skills/ha-control/SKILL.md) · [Changelog](CHANGELOG.md)

## Update and rollback

Update the integration in HACS and the companion in the app store, then restart Core when HACS requests it. Back up both before updating. The app backup contains its private bridge identity and file revisions. Restoring a different identity is reconciled through Supervisor Discovery.

For version rollback, reinstall the previous HACS release and restore the previous companion backup/image. During migration from the original local app, stop it before starting the repository app because both use port 8213. Copy the external MCP token and HTTP profiles into the new app; its internal identity is paired automatically. Preserve the old app backup until the new installation is verified.
