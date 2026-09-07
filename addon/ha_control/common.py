"""Shared validation, bounded results and secret redaction."""

import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from pathlib import Path

CONTRACTS = json.loads(Path(__file__).with_name("contracts.json").read_text())
MAX_OUTPUT = 16 * 1024


class ControlError(Exception):
    def __init__(self, code, message, *, uncertain=False, details=None):
        super().__init__(message)
        self.code, self.uncertain, self.details = code, uncertain, details

    def as_dict(self):
        return {"ok": False, "error": {"code": self.code, "message": str(self),
                "uncertain": self.uncertain, "details": self.details}}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Redactor:
    # Entity names such as sensor.token_count are data, not secret-bearing fields.
    secret_key = re.compile(r"(?:^|_)(?:password|passwd|token|authorization|api_key|client_secret|secret|secrets)$", re.I)

    def __init__(self, secrets=()):
        self.secrets = sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True)

    def __call__(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if self.secret_key.search(str(k)) else self(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]", value)
            value = re.sub(r"(?im)^(\s*(?:password|token|access_token|api_key|client_secret|ha_token|mcp_token|bridge_token)\s*[:=]\s*).+$", r"\1[REDACTED]", value)
        return value


def pointer_get(value, pointer):
    if pointer and not pointer.startswith("/"):
        raise ControlError("invalid_pointer", "Use an RFC6901 pointer beginning with /.")
    try:
        for key in pointer.split("/")[1:]:
            key = key.replace("~1", "/").replace("~0", "~")
            value = value[int(key)] if isinstance(value, list) else value[key]
    except (KeyError, IndexError, ValueError, TypeError):
        raise ControlError("invalid_pointer", "Pointer does not identify a value.") from None
    return value


class Results:
    """Bound memory and output independently; never hide truncation or replay work."""

    def __init__(self, redactor, ttl=900, max_bytes=64 * 1024 * 1024, clock=time.monotonic):
        self.redactor, self.ttl, self.max_bytes, self.clock = redactor, ttl, max_bytes, clock
        self.items = OrderedDict()
        self.bytes = 0

    def purge(self):
        now = self.clock()
        for key, (expiry, _, size) in list(self.items.items()):
            if expiry <= now:
                self.items.pop(key)
                self.bytes -= size

    def put(self, value):
        self.purge()
        value = self.redactor(value)
        size = len(encoded(value))
        if size > self.max_bytes:
            raise ControlError("result_too_large", "Result exceeds storage limit; narrow the upstream query.")
        while self.items and self.bytes + size > self.max_bytes:
            _, (_, _, old_size) = self.items.popitem(last=False)
            self.bytes -= old_size
        key = uuid.uuid4().hex
        self.items[key] = (self.clock() + self.ttl, value, size)
        self.bytes += size
        return key

    def pack(self, value):
        value = self.redactor(value)
        if len(encoded(value)) <= MAX_OUTPUT - 512 and not self._oversized_collection(value):
            return {"ok": True, "data": value, "truncated": False}
        return self.read(self.put(value))

    def _oversized_collection(self, value):
        if isinstance(value, (list, dict)):
            return len(value) > 50 or any(self._oversized_collection(v) for v in (value.values() if isinstance(value, dict) else value))
        return False

    def read(self, result_id, pointer="", offset=0, limit=50, fields=None):
        self.purge()
        if result_id not in self.items:
            raise ControlError("result_expired", "Result expired or was evicted; the operation was not repeated.")
        expiry, full, _ = self.items[result_id]
        value = pointer_get(full, pointer)
        limit = min(max(1, limit), 50)
        next_offset = None
        kind = type(value).__name__
        if isinstance(value, str):
            # Text offset is in characters, unlike collection offset.
            total = len(value)
            page = value[offset:offset + min(12000, limit * 240)]
            count = len(page)
        elif isinstance(value, dict):
            total = len(value)
            keys = list(value)[offset:offset + limit]
            page = {k: value[k] for k in keys}
            count = len(keys)
        elif isinstance(value, list):
            total = len(value)
            page = value[offset:offset + limit]
            count = len(page)
        else:
            page, total, count = value, 1, 1
        if fields:
            def select(v):
                return {k: v[k] for k in fields if k in v} if isinstance(v, dict) else v
            page = [select(v) for v in page] if isinstance(page, list) else select(page)
        # A large individual item remains discoverable via JSON Pointer.
        omitted = []
        def bounded(v, path):
            if isinstance(v, (list, dict)) and (len(v) > 50 or len(encoded(v)) > 10000):
                omitted.append(path)
                return {"$pointer": path, "type": type(v).__name__, "count": len(v)}
            if isinstance(v, str) and len(v.encode()) > 10000:
                omitted.append(path)
                return {"$pointer": path, "type": "str", "characters": len(v)}
            return v
        if isinstance(page, list):
            page = [bounded(v, f"{pointer}/{offset + i}") for i, v in enumerate(page)]
        elif isinstance(page, dict):
            page = {k: bounded(v, pointer + "/" + k.replace("~", "~0").replace("/", "~1")) for k, v in page.items()}
        while len(encoded(page)) > MAX_OUTPUT - 1500 and count > 1:
            count = max(1, count // 2)
            if isinstance(page, dict):
                page = dict(list(page.items())[:count])
            else:
                page = page[:count]
        if offset + count < total:
            next_offset = offset + count
        return {"ok": True, "data": page, "result_id": result_id, "pointer": pointer,
                "kind": kind, "total": total, "offset": offset, "next_offset": next_offset,
                "truncated": bool(next_offset is not None or omitted), "nested_pointers": omitted,
                "expires_in": max(0, int(expiry - self.clock()))}
