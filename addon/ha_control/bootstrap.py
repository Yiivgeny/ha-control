"""Private persistent identity for Supervisor-mediated Core discovery."""

import os
from pathlib import Path
import secrets


def bridge_identity(data_dir):
    """Create once; never rotate on restart or publish in app options."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bridge_token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            token = source.read(257).strip()
        if not 32 <= len(token) <= 256 or not all(c.isascii() and (c.isalnum() or c in "-_") for c in token):
            raise ValueError("Invalid persisted bridge identity; restore the app backup.")
        return token
    token = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w") as target:
        target.write(token + "\n")
        target.flush()
        os.fsync(target.fileno())
    return token
