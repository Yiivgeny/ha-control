"""Mounted configuration edits with optimistic concurrency and reversible revisions."""

import asyncio
import difflib
import json
import os
import tempfile
import uuid
from pathlib import Path

from .common import ControlError, digest


class Files:
    def __init__(self, roots, revisions, core_stopped):
        self.roots = {k: Path(v).resolve() for k, v in roots.items()}
        self.revisions = Path(revisions)
        self.revisions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.core_stopped = core_stopped
        self.lock = asyncio.Lock()

    def resolve(self, name):
        parts = name.split("/")
        if parts[0] not in self.roots or any(p in {"..", "."} for p in parts) or "\0" in name:
            raise ControlError("invalid_path", "Use an allowed root and a relative path without dot segments.")
        root = self.roots[parts[0]]
        p = root.joinpath(*parts[1:])
        # Reject symlinks rather than guessing which mapped root owns their target.
        for parent in [p, *list(p.parents)[:len(parts) - 1]]:
            if parent.is_symlink():
                raise ControlError("symlink", "Symlink paths are not writable or readable through this tool.")
        resolved = p.resolve()
        if not resolved.is_relative_to(root):
            raise ControlError("invalid_path", "Path escapes its configured mount.")
        return resolved

    @staticmethod
    def _read(path):
        if not path.exists():
            return None
        if not path.is_file():
            raise ControlError("not_file", "This action requires a regular file.")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ControlError("file_too_large", "File exceeds the 8 MiB configuration editing limit.")
        return path.read_bytes()

    @staticmethod
    def _atomic(path, data, mode=0o600):
        fd, tmp = tempfile.mkstemp(prefix=".ha-control-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def execute(self, action, path, apply=False, **args):
        p = self.resolve(path)
        destination = self.resolve(args["destination"]) if args.get("destination") else None
        if action not in {"list", "read"}:
            for target in [p, destination]:
                if target and ".storage" in target.parts and not await self.core_stopped():
                    raise ControlError("core_running", "Direct .storage edits require Core to be stopped. Prefer HA APIs.")
        async with self.lock:
            return await asyncio.to_thread(self._execute, action, p, path, destination, apply, args)

    def _execute(self, action, p, path, destination, apply, args):
        if action == "list":
            if not p.is_dir():
                raise ControlError("not_directory", "Directory does not exist.")
            return [{"name": c.name, "directory": c.is_dir(), "symlink": c.is_symlink(),
                     "size": c.lstat().st_size} for c in sorted(p.iterdir())]
        if action == "mkdir":
            if p.exists():
                raise ControlError("already_exists", "Path already exists.")
            if apply:
                p.mkdir(mode=0o755)  # Parent must already exist; no implicit tree mutations.
            return {"path": path, "applied": apply, "action": action}
        before = self._read(p)
        old_hash = digest(before) if before is not None else "missing"
        if action == "read":
            if before is None:
                raise ControlError("missing_file", "File does not exist.")
            try:
                text = before.decode("utf-8")
            except UnicodeError:
                raise ControlError("binary_file", "Only UTF-8 configuration files are editable.") from None
            return {"path": path, "hash": old_hash, "content": text}
        if args.get("expected_hash") != old_hash:
            raise ControlError("conflict", "expected_hash must match the current file; read it again before editing.", details={"current_hash": old_hash})
        if p in self.roots.values():
            raise ControlError("mount_root", "Mount roots cannot be modified.")
        mode = p.stat().st_mode & 0o777 if before is not None else 0o600
        after = before
        if action == "write":
            if "content" not in args:
                raise ControlError("missing_content", "write requires content.")
            if "[REDACTED]" in args["content"]:
                raise ControlError("redacted_content", "Do not write back redacted content. Patch unrelated fragments to preserve secrets.")
            after = args["content"].encode()
        elif action == "patch":
            if before is None or not args.get("edits"):
                raise ControlError("invalid_patch", "patch requires an existing file and edits.")
            text = before.decode("utf-8")
            for edit in args["edits"]:
                if text.count(edit["old"]) != 1:
                    raise ControlError("ambiguous_patch", "Each old fragment must occur exactly once.")
                text = text.replace(edit["old"], edit["new"], 1)
            after = text.encode()
        elif action == "delete":
            if before is None:
                raise ControlError("missing_file", "File does not exist.")
            after = None
        elif action == "move":
            if before is None or destination is None:
                raise ControlError("invalid_move", "move requires an existing source and destination.")
            if destination.exists():
                raise ControlError("destination_exists", "Move never overwrites an existing destination.")
            if not destination.parent.is_dir():
                raise ControlError("missing_parent", "Destination parent does not exist.")
        elif action == "restore":
            key = args.get("revision_id", "")
            if len(key) != 32 or any(c not in "0123456789abcdef" for c in key):
                raise ControlError("invalid_revision", "Invalid revision ID.")
            meta_path = self.revisions / (key + ".json")
            if not meta_path.exists():
                raise ControlError("missing_revision", "Revision does not exist.")
            meta = json.loads(meta_path.read_text())
            if meta["path"] != path:
                raise ControlError("revision_path", "Revision belongs to a different path.")
            after = (self.revisions / (key + ".bin")).read_bytes() if meta["existed"] else None
            mode = meta["mode"]
        if after is not None and len(after) > 8 * 1024 * 1024:
            raise ControlError("file_too_large", "File exceeds 8 MiB.")
        old_text = (before or b"").decode("utf-8")
        new_text = (after or b"").decode("utf-8")
        diff = "".join(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True), fromfile=path, tofile=args.get("destination", path)))
        result = {"path": path, "action": action, "applied": apply, "before_hash": old_hash,
                  "after_hash": digest(after) if after is not None else "missing", "diff": diff}
        if action == "move":
            result["destination"] = args["destination"]
        if not apply:
            return result
        key = uuid.uuid4().hex
        meta = {"path": path, "existed": before is not None, "mode": mode, "action": action}
        self._atomic(self.revisions / (key + ".json"), json.dumps(meta).encode())
        if before is not None:
            self._atomic(self.revisions / (key + ".bin"), before)
        # Optimistic concurrency: recheck immediately before replacing the file.
        current = self._read(p)
        if (digest(current) if current is not None else "missing") != old_hash:
            raise ControlError("conflict", "File changed while preparing the edit.")
        if action == "move":
            self._atomic(destination, before, mode)
            p.unlink()
        elif after is None:
            if p.exists():
                p.unlink()
        else:
            self._atomic(p, after, mode)
        result["revision_id"] = key
        return result
