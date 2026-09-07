"""Transport-independent execution engine, shared by MCP and the HA LLM bridge."""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from jsonschema import Draft202012Validator

from .common import CONTRACTS, MAX_OUTPUT, ControlError, Redactor, Results, encoded
from .files import Files
from .websocket import HASocket

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text())
ROOTS = {"homeassistant": "/homeassistant", "addons": "/addons",
         "addon_configs": "/addon_configs", "share": "/share", "media": "/media"}


class Engine:
    def __init__(self, options, data_dir="/data", roots=None):
        self.options = options
        self.supervisor_url = options.get("supervisor_url", "http://supervisor").rstrip("/")
        self.supervisor_token = os.environ.get("SUPERVISOR_TOKEN", options.get("supervisor_token", ""))
        # Explicit Core URLs/tokens are only for the standalone test harness.
        # Supervisor tokens authenticate to its proxies, not directly to Core.
        self.ha_url = options.get("ha_url", self.supervisor_url + "/core").rstrip("/")
        self.ha_token = options.get("ha_token", "") if "ha_url" in options else self.supervisor_token
        self.ws_url = (self.ha_url + "/api/websocket" if "ha_url" in options else self.supervisor_url + "/core/websocket").replace("https://", "wss://").replace("http://", "ws://")
        self.profiles = {p["name"]: p for p in options.get("http_profiles", [])}
        secrets = [self.supervisor_token, self.ha_token, options.get("mcp_token"), options.get("bridge_token")]
        for profile in self.profiles.values():
            secrets.extend(profile.get(k) for k in ["password", "bearer_token"])
        self.redact = Redactor(secrets)
        self.results = Results(self.redact)
        self.files = Files(roots or ROOTS, Path(data_dir) / "revisions", self.core_stopped)
        self.session = None
        self.socket = None
        self.validators = {c["name"]: Draft202012Validator(c["inputSchema"]) for c in CONTRACTS}
        self.audit_path = Path(data_dir) / "audit.jsonl"
        self.maintenance = None
        self.discovery_task = None
        self.core_stop_confirmed = False
        self.core_lifecycle_lock = asyncio.Lock()

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=False)
        self.socket = HASocket(self.session, self.ws_url, self.ha_token)
        self.maintenance = asyncio.create_task(self._maintain())
        if self.supervisor_token and self.options.get("bridge_token"):
            self.discovery_task = asyncio.create_task(self._publish_discovery())

    async def close(self):
        if self.discovery_task:
            self.discovery_task.cancel()
            await asyncio.gather(self.discovery_task, return_exceptions=True)
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
        if self.socket:
            await self.socket.close()
        if self.session:
            await self.session.close()

    async def _publish_discovery(self):
        # Supervisor persists and de-duplicates this declaration, and delivers
        # it again when Core starts. This retry never replays a user operation.
        while True:
            try:
                info = await self._http("supervisor", "/addons/self/info")
                hostname = info["data"]["hostname"]
                if not hostname or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in hostname):
                    raise ValueError("Invalid app hostname")
                await self._http("supervisor", "/discovery", method="POST", body={
                    "service": "ha_control", "config": {
                        "addon_url": f"http://{hostname}:8213",
                        "bridge_token": self.options["bridge_token"],
                    }})
                return
            except (ControlError, KeyError, ValueError):
                await asyncio.sleep(30)

    async def _maintain(self):
        while True:
            await asyncio.sleep(60)
            self.results.purge()
            for key, sub in list(self.socket.subscriptions.items()):
                if time.monotonic() - sub["touched"] > 900:
                    await self.socket._remove(key)

    async def invoke(self, name, arguments):
        started = time.monotonic()
        try:
            if name not in self.validators:
                raise ControlError("unknown_tool", "Unknown tool name.")
            errors = list(self.validators[name].iter_errors(arguments))
            if errors:
                # Validation error messages may echo secrets from invalid input.
                raise ControlError("invalid_arguments", "Arguments do not match the tool schema.",
                                   details=[{"path": list(e.absolute_path), "rule": e.validator} for e in errors[:5]])
            if name == "ha_result":
                result = self.results.read(**arguments)
            else:
                handlers = {"ha_discover": self.discover, "ha_request": self.request,
                            "ha_files": self.files.execute, "ha_events": self.socket.events}
                if name == "ha_files":
                    async with self.core_lifecycle_lock:
                        data = await handlers[name](**arguments)
                else:
                    data = await handlers[name](**arguments)
                result = self.results.pack(data)
        except ControlError as exc:
            result = self.redact(exc.as_dict())
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            result = ControlError("invalid_operation", "Operation failed; verify the discovered schema, path and payload.").as_dict()
        except Exception:
            # Do not log exception repr or request bodies: these can contain credentials.
            result = ControlError("internal_error", "Unexpected execution error; see the audit outcome and server version.").as_dict()
        if not result["ok"] and len(encoded(result)) > MAX_OUTPUT:
            details = result["error"].get("details")
            result["error"]["message"] = result["error"]["message"][:2048]
            try:
                result["error"]["details"] = {"result_id": self.results.put(details), "truncated": True}
            except ControlError:
                result["error"]["details"] = {"truncated": True, "reason": "error details exceed storage limit"}
        await asyncio.to_thread(self._audit, name, result.get("error", {}).get("code", "ok"), started)
        return result

    def _audit(self, name, status, started):
        record = {"time": time.time(), "tool": name, "outcome": status, "duration_ms": round((time.monotonic() - started) * 1000)}
        try:
            if self.audit_path.exists() and self.audit_path.stat().st_size > 2 * 1024 * 1024:
                self.audit_path.replace(self.audit_path.with_suffix(".previous.jsonl"))
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass  # Audit disk exhaustion must not turn a completed mutation into a failure.

    async def core_stopped(self):
        # Supervisor 2026.08 /core/info has no container-state field. Require a
        # successful explicit stop through this engine, then reassert it before
        # .storage editing. Never infer "stopped" from an unreachable Core.
        if not self.core_stop_confirmed:
            return False
        try:
            await self._http("supervisor", "/core/stop", method="POST", body={}, timeout=120)
            return True
        except ControlError:
            return False

    async def request(self, requests=None, **request):
        if requests is not None:
            if request:
                raise ControlError("invalid_batch", "Use requests alone, without single-request fields.")
            outputs = []
            for index, item in enumerate(requests):
                if "requests" in item:
                    raise ControlError("invalid_batch", "Nested batches are not supported.")
                errors = list(self.validators["ha_request"].iter_errors(item))
                if errors:
                    raise ControlError("invalid_batch", f"Batch item {index} does not match the request schema.")
                if not item.get("transport") or not item.get("operation"):
                    raise ControlError("invalid_batch", f"Batch item {index} requires transport and operation.")
            deadline = time.monotonic() + 120
            for index, item in enumerate(requests):
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        outputs.extend({"index": j, "ok": False, "error": {"code": "not_executed", "message": "Batch time budget exhausted."}} for j in range(index, len(requests)))
                        break
                    data = await self._request(**{**item, "timeout": min(item.get("timeout", 30), remaining)})
                    outputs.append({"index": index, "ok": True, "data": data})
                except ControlError as exc:
                    outputs.append({"index": index, **exc.as_dict()})
                    # An uncertain mutation stops subsequent actions with possible dependencies.
                    if exc.uncertain:
                        outputs.extend({"index": j, "ok": False, "error": {"code": "not_executed", "message": "Previous operation has an uncertain outcome."}} for j in range(index + 1, len(requests)))
                        break
            return outputs
        return await self._request(**request)

    async def _request(self, transport=None, operation=None, params=None, method="GET", body=None, profile=None, content_type="application/json", timeout=30):
        if not transport or not operation:
            raise ControlError("missing_operation", "transport and operation are required.")
        if transport == "websocket":
            if operation.startswith("subscribe_") or operation.endswith("/subscribe") or operation == "unsubscribe_events":
                raise ControlError("use_events", "Use ha_events to manage a bounded subscription.")
            return await self.socket.call(operation, params, timeout)
        if transport == "supervisor" and operation.split("?")[0] in {"/core/stop", "/core/start", "/core/restart", "/core/rebuild", "/core/update"} and method == "POST":
            async with self.core_lifecycle_lock:
                self.core_stop_confirmed = False
                result = await self._http(transport, operation, params, method, body, profile, content_type, timeout)
                self.core_stop_confirmed = operation == "/core/stop"
                return result
        return await self._http(transport, operation, params, method, body, profile, content_type, timeout)

    async def _http(self, transport, operation, params=None, method="GET", body=None, profile=None, content_type="application/json", timeout=30):
        parsed = urlsplit(operation)
        decoded = unquote(parsed.path)
        if parsed.scheme or parsed.netloc or not operation.startswith("/") or operation.startswith("//") or "\\" in decoded or any(p in {".", ".."} for p in decoded.split("/")):
            raise ControlError("invalid_path", "HTTP operation must be an origin-relative path without dot segments.")
        # This generic transport decodes JSON, text and binary responses. A JSON
        # preference rejects Supervisor journal endpoints before logs are read.
        headers = {"Accept": "*/*"}
        if transport == "rest":
            base, token = self.ha_url, self.ha_token
        elif transport == "supervisor":
            base, token = self.supervisor_url, self.supervisor_token
            if not token:
                raise ControlError("supervisor_unavailable", "Supervisor credentials are not available outside the add-on.")
        elif transport == "http":
            if profile not in self.profiles:
                raise ControlError("unknown_profile", "Choose a configured HTTP profile via ha_discover profiles.")
            config = self.profiles[profile]
            base, token = config["url"].rstrip("/"), config.get("bearer_token")
            if config.get("username"):
                raw = (config["username"] + ":" + config.get("password", "")).encode()
                headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            raise ControlError("unknown_transport", "Use websocket, rest, supervisor or http.")
        if token:
            headers["Authorization"] = "Bearer " + token
        options = {"params": params, "headers": headers, "allow_redirects": False,
                   "timeout": aiohttp.ClientTimeout(total=timeout)}
        if body is not None:
            headers["Content-Type"] = content_type
            if content_type == "application/json":
                options["json"] = body
            elif content_type == "application/x-www-form-urlencoded" and isinstance(body, dict):
                options["data"] = body
            elif isinstance(body, str):
                options["data"] = body.encode()
            else:
                raise ControlError("invalid_body", "Non-JSON bodies must be text, or an object for URL-encoded forms.")
        try:
            async with self.session.request(method, base + operation, **options) as response:
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 32 * 1024 * 1024:
                        raise ControlError("response_too_large", "Response exceeds 32 MiB; narrow the upstream query.", uncertain=method not in {"GET", "HEAD"})
                    chunks.append(chunk)
                raw = b"".join(chunks)
                mime = response.headers.get("Content-Type", "").split(";")[0]
                try:
                    value = json.loads(raw) if raw else None
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if mime.startswith(("image/", "audio/", "video/")) or mime == "application/octet-stream":
                        value = {"content_type": mime, "base64": base64.b64encode(raw).decode()}
                    else:
                        value = raw.decode("utf-8", errors="replace")
                if not 200 <= response.status < 300:
                    raise ControlError("http_error", f"Upstream returned HTTP {response.status}.", details={"status": response.status, "body": value})
                if transport == "supervisor" and isinstance(value, dict) and value.get("result") == "error":
                    raise ControlError("supervisor_error", "Supervisor rejected the operation.", details=value)
                return value
        except ControlError:
            raise
        except aiohttp.ClientConnectorError:
            raise ControlError("upstream_unavailable", "Unable to connect to the configured upstream.") from None
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ControlError("transport_error", "Upstream response was interrupted; inspect state before retrying.", uncertain=method not in {"GET", "HEAD"}) from None

    async def discover(self, scope="overview", query="", operation=None, offset=0, limit=50):
        if scope == "overview":
            try:
                core = await self.socket.call("get_config", timeout=5)
                core = {k: core.get(k) for k in ["version", "location_name", "time_zone"]}
            except ControlError as exc:
                core = exc.as_dict()
            return {"home": core.get("location_name", "Home Assistant"), "core": core, "scopes": ["entities", "services", "websocket", "rest", "supervisor", "profiles"],
                    "file_roots": list(self.files.roots), "supervisor_available": bool(self.supervisor_token),
                    "http_profiles": list(self.profiles), "catalog_version": CATALOG["version"],
                    "workflow": "Discover an operation, inspect its schema, request it, verify the result. Large outputs expose result_id and JSON pointers."}
        if scope == "websocket":
            return await self.socket.call("ha_control/catalog", {"query": query, "operation": operation, "offset": offset, "limit": limit})
        if scope == "services":
            services = await self.socket.call("get_services")
            rows = [{"operation": f"{domain}.{name}", "description": spec.get("description", ""), "name": spec.get("name", name)}
                    for domain, values in services.items() for name, spec in values.items()]
            if operation:
                try:
                    domain, name = operation.split(".", 1)
                    spec = services[domain][name]
                except (ValueError, KeyError):
                    raise ControlError("operation_missing", "Service is not registered.") from None
                return {"operation": operation, "transport": "websocket", "command": "call_service",
                        "params": {"domain": domain, "service": name, "service_data": {}, "target": {}}, "schema": spec}
        elif scope == "entities":
            states = await self.socket.call("get_states")
            if operation:
                for state in states:
                    if state["entity_id"] == operation:
                        return state
                raise ControlError("entity_missing", "Entity does not exist in the current state machine.")
            rows = [{"entity_id": s["entity_id"], "state": s["state"], "name": s.get("attributes", {}).get("friendly_name")} for s in states]
        elif scope == "profiles":
            rows = [{k: p[k] for k in ["name", "description", "database"] if k in p} for p in self.profiles.values()]
        elif scope in {"rest", "supervisor"}:
            rows = CATALOG[scope]
        else:
            raise ControlError("unknown_scope", "Unknown discovery scope.")
        if operation:
            rows = [r for r in rows if r.get("operation", r.get("name")) == operation]
        elif query:
            rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
        return {"items": rows[offset:offset + limit], "total": len(rows), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(rows) else None}
