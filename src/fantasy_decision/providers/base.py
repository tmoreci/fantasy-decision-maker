"""Shared provider plumbing.

Every provider is expected to fail softly. A dead scoreboard endpoint should cost
us the market signal and nothing else: the run continues, and the gap is recorded
in the dossier's `missing` list so Jev is told the signal is absent rather than
being left to assume it was neutral.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

import httpx

from fantasy_decision.cache import DiskCache

logger = logging.getLogger("fantasy_decision.providers")

T = TypeVar("T")


class ProviderError(RuntimeError):
    """A provider could not supply data. Always caught by `soft_call`."""


class HttpProvider:
    """A JSON-over-HTTP provider with caching and a single retry."""

    name = "provider"
    base_url = ""

    def __init__(
        self,
        cache: DiskCache | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.cache = cache or DiskCache()
        self._client = client
        self._owns_client = client is None
        self.timeout = timeout

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def get_json(self, url: str, *, ttl_seconds: float = 300.0, params: dict[str, Any] | None = None) -> Any:
        cache_key = url if not params else f"{url}?{sorted(params.items())}"
        cached = self.cache.get(cache_key, ttl_seconds)
        if cached is not None:
            logger.debug("%s cache hit: %s", self.name, cache_key)
            return cached

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                logger.debug("%s request failed (attempt %d): %s", self.name, attempt + 1, error)
                continue
            self.cache.set(cache_key, payload)
            return payload

        raise ProviderError(f"{self.name}: GET {url} failed: {last_error}") from last_error

    def __enter__(self) -> "HttpProvider":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def soft_call(label: str, fn: Callable[[], T], missing: list[str]) -> T | None:
    """Run `fn`, and on any provider failure record `label` as missing data.

    This is the mechanism behind graceful degradation: callers get `None` and the
    dossier gains an explicit, model-visible note about what could not be gathered.
    """
    try:
        return fn()
    except ProviderError as error:
        logger.info("unavailable: %s (%s)", label, error)
        missing.append(label)
        return None
    except Exception as error:  # noqa: BLE001 - a provider bug must not kill the run
        logger.warning("provider raised unexpectedly for %s: %s", label, error)
        missing.append(label)
        return None
