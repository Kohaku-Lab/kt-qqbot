"""``qq_status`` plugin: live QQ chat status for the dashboard side panel.

It owns the ``plugin:qq_status`` panel: for its creature's ``qq_feed``
triggers it reports the chats, their last messages, free passive-reply
slots and the hub connection state, and pushes a new state on every hub
change.
"""

import asyncio

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.errors import BadOwnerArgument
from kt_qqbot.hub import QQHub
from kt_qqbot.records import split_conv_id
from kt_qqbot.render import CHAT_TYPES
from kt_qqbot.trigger import QQFeedTrigger, feeds_of

logger = get_logger("kt_qqbot.status")

PREVIEW_CHARS = 120
RESCAN_S = 2.0
DEBOUNCE_S = 0.2


def _preview(message: dict) -> dict:
    text = str(message.get("content", ""))
    if len(text) > PREVIEW_CHARS:
        text = text[:PREVIEW_CHARS] + "…"
    return {
        "tag": f"m{message.get('seq')}",
        "sender": "you" if message.get("self") else message.get("alias") or "?",
        "text": text,
        "ts": message.get("ts"),
        "self": bool(message.get("self")),
    }


class QQStatusPlugin(BasePlugin):
    """Report the creature's QQ feeds to its side panel."""

    name = "qq_status"
    priority = 90
    description = "Show this creature's QQ chats in the dashboard side panel."

    def __init__(self, max_chats: int = 30, last_messages: int = 5):
        super().__init__()
        self.max_chats = int(max_chats)
        self.last_messages = int(last_messages)
        self._ctx: PluginContext | None = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._hubs: set[QQHub] = set()
        self._last: dict | None = None

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context

    def _feeds(self) -> list[QQFeedTrigger]:
        agent = self._ctx.host_agent if self._ctx is not None else None
        return feeds_of(agent)

    def _on_hub_change(self, kind: str, cid: str) -> None:
        self._changed.set()

    def _track_hubs(self, feeds: list[QQFeedTrigger]) -> None:
        live = {f.hub for f in feeds if f.hub is not None}
        for hub in live - self._hubs:
            hub.add_listener(self._on_hub_change)
        for hub in self._hubs - live:
            hub.remove_listener(self._on_hub_change)
        self._hubs = live

    # ── UI owner ───────────────────────────────────────────────────

    def ui_state(self) -> dict:
        """Return one entry per feed with its chats, newest activity first."""
        return {"feeds": [self._feed_state(f) for f in self._feeds()]}

    def ui_call(self, action: str, args: dict) -> dict:
        """``refresh`` returns and pushes the current state."""
        if action != "refresh":
            raise BadOwnerArgument(f"unknown action {action!r}")
        state = self.ui_state()
        self.ui_emit("state", state)
        return state

    def _feed_state(self, feed: QQFeedTrigger) -> dict:
        hub = feed.hub
        store = hub.store
        chats = []
        for cid in store.conversations():
            if not feed.matches(cid):
                continue
            kind = split_conv_id(cid)[0]
            last = store.recent(cid, self.last_messages)
            chats.append(
                {
                    "handle": store.handle(cid),
                    "type": CHAT_TYPES.get(kind, kind),
                    "seen": feed.known(cid),
                    "unread": feed.unread(cid),
                    "free_slots": hub.ledger.free_slots(cid, kind),
                    "last": [_preview(m) for m in last],
                    "last_ts": last[-1].get("ts", 0) if last else 0,
                }
            )
        chats.sort(key=lambda c: c["last_ts"] or 0, reverse=True)
        spec = feed.conversations
        return {
            "bot": hub.app_id,
            "gateway": hub.status,
            "subscriber": feed.subscriber,
            "conversations": spec if isinstance(spec, str) else list(spec),
            "wake": feed.policy.mode,
            "total_chats": len(chats),
            "chats": chats[: self.max_chats],
        }

    # ── live pushes ────────────────────────────────────────────────

    async def on_agent_start(self) -> None:
        self._task = asyncio.create_task(self._watch())

    async def on_agent_stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._track_hubs([])

    async def _watch(self) -> None:
        while True:
            hubs_before = set(self._hubs)
            self._track_hubs(self._feeds())
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=RESCAN_S)
            except asyncio.TimeoutError:
                if self._hubs == hubs_before and self._last is not None:
                    continue
            await asyncio.sleep(DEBOUNCE_S)
            self._changed.clear()
            try:
                state = self.ui_state()
            except Exception as exc:
                logger.warning("qq_status state failed", error=str(exc))
                continue
            if state != self._last:
                self._last = state
                self.ui_emit("state", state)
