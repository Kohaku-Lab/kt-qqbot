"""Image search backends for ``qq_image_search``.

:class:`BraveImageSearch` uses the Brave Search API (``BRAVE_API_KEY``) with
strict safe search. :class:`DuckDuckGoImageSearch` uses DuckDuckGo's
unofficial image endpoint (token page, then ``i.js``) as a best-effort
fallback; it can break without notice.
"""

import os
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/images/search"
DDG_HOME = "https://duckduckgo.com/"
DDG_IMAGES = "https://duckduckgo.com/i.js"
REQUEST_TIMEOUT_S = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; kt-qqbot)"
_VQD = re.compile(r"""vqd=["']?([\d-]+)""")


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
    """DuckDuckGo images (unofficial): fetch a ``vqd`` token, then ``i.js``."""

    name = "duckduckgo"

    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def search(self, query: str, count: int) -> list[ImageResult]:
        """Return up to ``count`` candidates with safe search on."""
        client = self.client or httpx.AsyncClient()
        headers = {"User-Agent": USER_AGENT, "Referer": DDG_HOME}
        try:
            page = await client.get(
                DDG_HOME,
                params={"q": query, "iax": "images", "ia": "images"},
                headers=headers,
                timeout=REQUEST_TIMEOUT_S,
            )
            match = _VQD.search(page.text)
            if match is None:
                raise ImageSearchError(
                    "DuckDuckGo image search broke (no vqd token); set BRAVE_API_KEY"
                )
            resp = await client.get(
                DDG_IMAGES,
                params={
                    "l": "wt-wt",
                    "o": "json",
                    "q": query,
                    "vqd": match.group(1),
                    "f": ",,,,,",
                    "p": "1",
                },
                headers=headers,
                timeout=REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ImageSearchError(f"DuckDuckGo image search failed: {exc}") from exc
        finally:
            if self.client is None:
                await client.aclose()
        results = []
        for item in body.get("results") or []:
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
    return DuckDuckGoImageSearch(client)
