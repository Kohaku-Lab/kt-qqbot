"""Image search backends for ``qq_image_search``.

:class:`BraveImageSearch` uses the Brave Search API (``BRAVE_API_KEY``) with
strict safe search. :class:`DuckDuckGoImageSearch` uses the ``ddgs``
library (the same one core ``web_search`` uses) with safe search on.
"""

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from ddgs import DDGS
from ddgs.exceptions import DDGSException

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"
REQUEST_TIMEOUT_S = 15.0
NO_RESULTS = "No results"


class ImageSearchError(RuntimeError):
    """An image search backend failed."""


@dataclass(frozen=True)
class ImageResult:
    """One image candidate: the full image, its thumbnail, title and page."""

    url: str
    thumbnail: str
    title: str
    source: str


class ImageSearchBackend(Protocol):
    """Anything that turns a query into image candidates."""

    name: str

    async def search(self, query: str, count: int) -> list[ImageResult]: ...


class BraveImageSearch:
    """Brave Search API images endpoint, strict safe search."""

    name = "brave"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client

    async def search(self, query: str, count: int) -> list[ImageResult]:
        """Return up to ``count`` candidates."""
        client = self.client or httpx.AsyncClient()
        try:
            resp = await client.get(
                BRAVE_ENDPOINT,
                params={"q": query, "count": count, "safesearch": "strict"},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key,
                },
                timeout=REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageSearchError(f"Brave image search failed: {exc}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        results = []
        for item in body.get("results") or []:
            full = (item.get("properties") or {}).get("url") or ""
            thumb = (item.get("thumbnail") or {}).get("src") or ""
            if full and thumb:
                results.append(
                    ImageResult(
                        full,
                        thumb,
                        str(item.get("title") or ""),
                        str(item.get("url") or ""),
                    )
                )
        return results[:count]


class DuckDuckGoImageSearch:
    """DuckDuckGo images through the ``ddgs`` library, safe search on."""

    name = "duckduckgo"

    def __init__(self, ddgs_factory: Callable[[], Any] = DDGS):
        self.ddgs_factory = ddgs_factory

    def _rows(self, query: str, count: int) -> list[dict]:
        with self.ddgs_factory() as ddgs:
            return ddgs.images(query, safesearch="on", max_results=count) or []

    async def search(self, query: str, count: int) -> list[ImageResult]:
        """Return up to ``count`` candidates; an empty list when nothing matched."""
        try:
            rows = await asyncio.to_thread(self._rows, query, count)
        except DDGSException as exc:
            if NO_RESULTS in str(exc):
                return []
            raise ImageSearchError(f"DuckDuckGo image search failed: {exc}") from exc
        results = []
        for item in rows:
            full, thumb = item.get("image") or "", item.get("thumbnail") or ""
            if full and thumb:
                results.append(
                    ImageResult(
                        full,
                        thumb,
                        str(item.get("title") or ""),
                        str(item.get("url") or ""),
                    )
                )
        return results[:count]


def default_backend(
    api_key: str | None = None, client: httpx.AsyncClient | None = None
) -> ImageSearchBackend:
    """Return Brave when a key is given or in ``BRAVE_API_KEY``, else DuckDuckGo."""
    key = api_key or os.environ.get("BRAVE_API_KEY", "").strip()
    if key:
        return BraveImageSearch(key, client)
    return DuckDuckGoImageSearch()
