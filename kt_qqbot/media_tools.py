"""``qq_sticker`` and ``qq_image_search``: find media the creature can look at, then send.

Both return handles (``sticker:<name>``, ``img:<n>``) plus low-detail
thumbnails; ``qq_send(media=...)`` resolves the handles.
"""

import base64
import mimetypes
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

import httpx

from kohakuterrarium.llm.message import ImagePart, TextPart
from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot import access, media
from kt_qqbot.handles import SEARCHES, owner_of
from kt_qqbot.image_search import (
    ImageResult,
    ImageSearchBackend,
    ImageSearchError,
    default_backend,
)
from kt_qqbot.stickers import StickerLibrary
from kt_qqbot.trigger import feeds_of

logger = get_logger("kt_qqbot.media_tools")

MAX_STICKER_RESULTS = 8
MAX_STICKER_THUMB_BYTES = 512 * 1024
MAX_IMAGE_RESULTS = 6
MAX_THUMB_BYTES = 1024 * 1024


def _thumbnail(data: bytes, mime: str, label: str) -> ImagePart:
    encoded = base64.b64encode(data).decode("ascii")
    return ImagePart(
        url=f"data:{mime};base64,{encoded}",
        detail="low",
        source_type="qq",
        source_name=label,
    )


class QQStickerTool(BaseTool):
    """Search the user's sticker folder."""

    needs_context = True

    def __init__(self, config: ToolConfig | None = None, **options: Any):
        super().__init__(config)
        self.library = StickerLibrary(options.get("directory"))
        self.max_results = min(int(options.get("max_results", 4)), MAX_STICKER_RESULTS)

    @property
    def tool_name(self) -> str:
        return "qq_sticker"

    @property
    def description(self) -> str:
        return (
            "Find stickers in the user's sticker folder by mood or words; returns "
            "sticker:<name> handles with previews. Not for searching the web."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        """Describe the arguments for native function calling."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Mood or words, e.g. 'happy cat'.",
                },
            },
        }

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return (
            "# qq_sticker\n\nFind stickers by mood or words. Each match is a "
            "`sticker:<name>` handle with a small preview; send one with "
            "`qq_send(media='sticker:<name>')`.\n"
        )

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Return matching sticker handles and previews."""
        if context is not None and not access.allowed(context):
            return ToolResult(error=access.ADMIN_ONLY)
        query = str(args.get("query") or "").strip()
        if not self.library.all():
            return ToolResult(
                output=(
                    "There are no stickers yet. The user can add images to "
                    f"{self.library.directory}."
                ),
                exit_code=0,
            )
        found = self.library.search(query, self.max_results)
        if not found:
            return ToolResult(output=f"No sticker matches {query!r}.", exit_code=0)
        lines = []
        images = []
        for sticker in found:
            label = f"sticker:{sticker.name}"
            mood = f" ({sticker.mood})" if sticker.mood else ""
            lines.append(f"{label}{mood}")
            if sticker.path.stat().st_size <= MAX_STICKER_THUMB_BYTES:
                mime = mimetypes.guess_type(sticker.path.name)[0] or "image/png"
                images.append(_thumbnail(sticker.path.read_bytes(), mime, label))
        text = "Stickers (previews follow, in order):\n" + "\n".join(lines)
        return ToolResult(output=[TextPart(text=text), *images], exit_code=0)


class QQImageSearchTool(BaseTool):
    """Search the web for images; return candidates to look at before sending."""

    needs_context = True

    def __init__(self, config: ToolConfig | None = None, **options: Any):
        super().__init__(config)
        self._backend: ImageSearchBackend | None = options.get("backend")
        self._api_key = options.get("api_key")
        self.http: httpx.AsyncClient | None = options.get("http")
        self.blocklist = [
            str(d).lower().lstrip(".") for d in options.get("blocklist") or []
        ]
        self.rate_limit = int(options.get("rate_limit_per_minute", 6))
        self.max_thumb_bytes = int(options.get("max_thumb_bytes", MAX_THUMB_BYTES))
        self._recent: dict[str, deque[float]] = {}

    @property
    def tool_name(self) -> str:
        return "qq_image_search"

    @property
    def description(self) -> str:
        return (
            "Search the web for images (safe search); returns img:<n> handles with "
            "previews to check before sending. Not for facts, use web_search."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        """Describe the arguments for native function calling."""
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for."},
                "count": {"type": "integer", "description": "1-6, default 4."},
            },
            "required": ["query"],
        }

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return (
            "# qq_image_search\n\nSearch images. Each candidate is an `img:<n>` "
            "handle with a small preview; look at the previews, then send one with "
            "`qq_send(media='img:<n>')`. Handles last 15 minutes and are replaced "
            "by the next search.\n"
        )

    @property
    def backend(self) -> ImageSearchBackend:
        """Return the configured backend (Brave with a key, else DuckDuckGo)."""
        if self._backend is None:
            self._backend = default_backend(self._api_key)
        return self._backend

    def _blocked(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.blocklist)

    def _rate_ok(self, key: str) -> bool:
        now = time.monotonic()
        recent = self._recent.setdefault(key, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= self.rate_limit:
            return False
        recent.append(now)
        return True

    async def _fetch_thumb(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[bytes, str] | None:
        try:
            await media.check_public_url(url)
            async with client.stream(
                "GET", url, timeout=15.0, follow_redirects=False
            ) as resp:
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "").split(";")[0].strip()
                if not mime.startswith("image/"):
                    return None
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_thumb_bytes:
                        return None
                    chunks.append(chunk)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("Thumbnail skipped", url=url, error=str(exc))
            return None
        return b"".join(chunks), mime

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Search, keep usable candidates, and return handles with previews."""
        if context is not None and not access.allowed(context):
            return ToolResult(error=access.ADMIN_ONLY)
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(error="query is required")
        try:
            count = max(1, min(int(args.get("count") or 4), MAX_IMAGE_RESULTS))
        except (TypeError, ValueError):
            return ToolResult(error=f"bad count {args.get('count')!r}")
        owner = owner_of(context)
        feeds = feeds_of(getattr(context, "agent", None))
        chat = feeds[0].last_delivered[0] if feeds and feeds[0].last_delivered else "*"
        if not self._rate_ok(f"{owner}|{chat}"):
            return ToolResult(
                error=f"too many image searches; at most {self.rate_limit} per minute"
            )
        try:
            candidates = await self.backend.search(query, count * 2)
        except ImageSearchError as exc:
            return ToolResult(error=str(exc))
        client = self.http or httpx.AsyncClient()
        kept: list[ImageResult] = []
        previews: list[ImagePart] = []
        try:
            for result in candidates:
                if len(kept) >= count:
                    break
                urls = (result.url, result.thumbnail)
                if not all(u.startswith("https://") for u in urls):
                    continue
                if any(self._blocked(u) for u in (*urls, result.source) if u):
                    continue
                thumb = await self._fetch_thumb(client, result.thumbnail)
                if thumb is None:
                    continue
                kept.append(result)
                previews.append(_thumbnail(thumb[0], thumb[1], f"img:{len(kept)}"))
        finally:
            if self.http is None:
                await client.aclose()
        SEARCHES.remember(owner, kept)
        if not kept:
            return ToolResult(
                output=f"no usable image found for {query!r}", exit_code=0
            )
        lines = [
            f"img:{i} {r.title[:80]} ({urlparse(r.source or r.url).hostname})"
            for i, r in enumerate(kept, start=1)
        ]
        text = "Image candidates (previews follow, in order):\n" + "\n".join(lines)
        return ToolResult(output=[TextPart(text=text), *previews], exit_code=0)
