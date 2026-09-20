"""Tiny disk cache with per-entry TTL.

Sleeper's player index is ~5MB and changes daily; scoreboards change every few
minutes during games. Caching is what makes it reasonable to hit four providers on
every run.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "fantasy-decision-maker"


class DiskCache:
    def __init__(self, directory: Path | None = None, *, enabled: bool = True) -> None:
        self.directory = directory or DEFAULT_CACHE_DIR
        self.enabled = enabled
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        if not self.enabled or ttl_seconds <= 0:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict) or "stored_at" not in payload:
            return None
        if time.time() - payload["stored_at"] > ttl_seconds:
            return None
        return payload.get("value")

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        try:
            self._path(key).write_text(json.dumps({"stored_at": time.time(), "value": value}))
        except (OSError, TypeError):
            # A cache write failure must never break a decision.
            pass

    def clear(self) -> int:
        if not self.enabled or not self.directory.exists():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
