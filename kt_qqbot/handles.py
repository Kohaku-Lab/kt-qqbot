"""Media handles ``qq_send`` resolves: chat attachments and recent image searches.

``m12.1`` is attachment 1 of message m12 (optionally ``<chat>/m12.1``);
``img:2`` is candidate 2 of the creature's latest ``qq_image_search``.
"""

import re
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kt_qqbot.image_search import ImageResult

_CHAT_HANDLE = re.compile(
    r"^(?:(?P<chat>[A-Za-z0-9_:.-]+)/)?m(?P<seq>\d+)\.(?P<idx>\d+)$"
)
SEARCH_TTL_S = 15 * 60
MAX_OWNERS = 256


def owner_of(context: Any) -> str:
    """Return the key that scopes search handles to one creature."""
    if context is None:
        return "default"
    return str(
        getattr(context, "creature_id", "")
        or getattr(context, "agent_name", "")
        or "default"
    )


def parse_chat_handle(spec: str) -> tuple[str | None, int, int] | None:
    """Return ``(chat or None, seq, attachment index)`` for ``[chat/]m<seq>.<n>``."""
    match = _CHAT_HANDLE.match(spec.strip())
    if match is None:
        return None
    return match.group("chat"), int(match.group("seq")), int(match.group("idx"))


class SearchHandles:
    """The latest image-search candidates per creature, valid for a while."""

    def __init__(self, ttl: float = SEARCH_TTL_S):
        self.ttl = ttl
        self._entries: dict[str, tuple[float, list["ImageResult"]]] = {}
        self._lock = threading.Lock()

    def remember(self, owner: str, results: list["ImageResult"]) -> None:
        """Replace ``owner``'s candidates; ``img:1`` is ``results[0]``."""
        with self._lock:
            self._entries.pop(owner, None)
            self._entries[owner] = (time.time(), list(results))
            while len(self._entries) > MAX_OWNERS:
                self._entries.pop(next(iter(self._entries)))

    def get(self, owner: str, number: int) -> "ImageResult | None":
        """Return candidate ``img:<number>`` of ``owner``, or None if gone."""
        with self._lock:
            entry = self._entries.get(owner)
        if entry is None or time.time() - entry[0] > self.ttl:
            return None
        results = entry[1]
        return results[number - 1] if 1 <= number <= len(results) else None


SEARCHES = SearchHandles()
