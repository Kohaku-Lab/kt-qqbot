"""``qq_feed`` trigger: wake the creature with new QQ chat as incremental updates.

Every delivery holds only messages this feed has not shown yet, one
``<qq_update>`` block per chat. A chat's first delivery also carries a
``<qq_backfill>`` block of earlier history. Cursors live in the bot's
on-disk :class:`~kt_qqbot.store.ChatStore`, keyed by ``subscriber``.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from kohakuterrarium.core.events import TriggerEvent
from kohakuterrarium.modules.trigger.base import BaseTrigger
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot import access
from kt_qqbot import hub as hub_registry
from kt_qqbot.hub import QQHub
from kt_qqbot.media import ImageOptions, images_for
from kt_qqbot.render import chat_attrs, combine, render_block, render_line
from kt_qqbot.wake import WakePolicy, is_direct

logger = get_logger("kt_qqbot.trigger")

IDLE_POLL_S = 1.0


def omitted_note(count: int) -> str:
    """Return the line that stands for messages left out of a delivery."""
    return f"({count} earlier messages not shown; use qq_history)"


@dataclass
class _Pending:
    """Running summary of one chat's unseen messages."""

    count: int = 0
    direct: bool = False
    newest: float = 0.0

    def add(self, message: dict) -> None:
        self.count += 1
        self.direct = self.direct or is_direct(message)
        self.newest = max(
            self.newest, float(message.get("recv") or message.get("ts") or 0)
        )


class QQFeedTrigger(BaseTrigger):
    """Deliver new chat from the selected QQ conversations."""

    resumable = True
    universal = True
    setup_tool_name = "qq_watch"
    setup_description = (
        "Start receiving QQ chat (all chats, private, groups, or listed chat handles)."
    )
    setup_param_schema = {
        "type": "object",
        "properties": {
            "conversations": {
                "anyOf": [
                    {"type": "string"},
                    {"type": "array", "items": {"type": "string"}},
                ],
                "description": "all, c2c, groups, or a list of chat handles.",
            },
            "wake": {
                "type": "string",
                "enum": ["mention", "all", "ambient"],
                "description": "When chat wakes you (default ambient).",
            },
        },
    }

    def __init__(
        self,
        prompt: str | None = None,
        *,
        bot: str | None = None,
        conversations: str | list[str] = "all",
        wake: str = "ambient",
        quiet_period: float = 20.0,
        every_n: int = 8,
        cooldown: float = 60.0,
        batch_delay: float = 1.0,
        backfill: int = 20,
        hold: bool = True,
        typing: bool = False,
        images: bool = True,
        image_detail: str = "auto",
        max_images: int = 8,
        subscriber: str | None = None,
        max_delivery: int = 100,
        unseen_history: str = "backfill",
        debug_raw_events: int = 0,
    ):
        super().__init__(
            prompt=prompt,
            bot=bot,
            conversations=conversations,
            wake=wake,
            quiet_period=quiet_period,
            every_n=every_n,
            cooldown=cooldown,
            batch_delay=batch_delay,
            backfill=backfill,
            hold=hold,
            typing=typing,
            images=images,
            image_detail=image_detail,
            max_images=max_images,
            subscriber=subscriber,
            max_delivery=max_delivery,
            unseen_history=unseen_history,
            debug_raw_events=debug_raw_events,
        )
        self.bot = bot
        self.conversations = conversations
        self.policy = WakePolicy(
            mode=wake,  # type: ignore[arg-type]
            quiet_period=float(quiet_period),
            every_n=int(every_n),
            cooldown=float(cooldown),
        )
        self.batch_delay = float(batch_delay)
        self.backfill = int(backfill)
        self.hold = bool(hold)
        self.typing = bool(typing)
        self.image_options = ImageOptions(
            enabled=bool(images),
            detail=image_detail,  # type: ignore[arg-type]
            max_per_delivery=int(max_images),
        )
        if unseen_history not in ("backfill", "new"):
            raise ValueError("unseen_history must be 'backfill' or 'new'")
        self.subscriber = subscriber
        self._explicit_subscriber = bool(subscriber)
        self.max_delivery = max(int(max_delivery), 1)
        self.debug_raw_events = max(int(debug_raw_events), 0)
        self.unseen_history = unseen_history
        self.hub: QQHub | None = None
        self.last_delivered: list[str] = []
        self.hold_bypass: set[str] = set()
        self._summaries: dict[str, _Pending] = {}
        self._last_ambient: dict[str, float] = {}
        self._wakeup = asyncio.Event()
        self._http: httpx.AsyncClient | None = None
        self._owns_hub = False
        self._started = time.time()
        self._tasks: set[asyncio.Task] = set()

    @classmethod
    def post_setup(cls, trigger: "QQFeedTrigger", context: Any) -> None:
        """Key the cursor of a tool-installed feed by the installing creature."""
        ident = getattr(context, "creature_id", "") or getattr(
            context, "agent_name", ""
        )
        if ident:
            trigger.bind_host(
                ident,
                getattr(context, "agent_name", ""),
                getattr(context, "agent", None),
            )
            if not trigger._explicit_subscriber:
                trigger.options["subscriber"] = trigger.subscriber
                trigger._explicit_subscriber = True

    def bind_host(self, creature_id: str, agent_name: str, agent: Any) -> None:
        """Record the hosting creature; the default cursor key becomes its id."""
        super().bind_host(creature_id, agent_name, agent)
        if not self._explicit_subscriber and self.host_creature_id:
            key = f"creature:{self.host_creature_id}"
            if key != self.subscriber:
                self.subscriber = key
                if self.hub is not None:
                    self._scan()

    def _default_subscriber(self) -> str:
        if self.host_creature_id:
            return f"creature:{self.host_creature_id}"
        assert self.hub is not None
        return f"feed:{self.hub.app_id}:{self._filter_key()}"

    # ── lifecycle ──────────────────────────────────────────────────

    async def attach(self, hub: QQHub) -> None:
        """Bind to an already-acquired hub (used by tests and the router)."""
        self.hub = hub
        self._prepare()

    def _prepare(self) -> None:
        hub = self.hub
        assert hub is not None
        if not self.subscriber:
            self.subscriber = self._default_subscriber()
        hub.subscribe(self._wakeup)
        hub.add_listener(self._on_hub_change)
        self._scan()

    def _scan(self) -> None:
        """Rebuild the pending summaries from the store (once per bind)."""
        hub = self.hub
        assert hub is not None
        self._summaries = {}
        for cid in hub.store.conversations():
            if self.matches(cid):
                summary = _Pending()
                for message in self.pending(cid):
                    summary.add(message)
                self._summaries[cid] = summary
        self._wakeup.set()

    def _on_hub_change(self, kind: str, cid: str) -> None:
        hub = self.hub
        if hub is None or not cid or not self.matches(cid):
            return
        summary = self._summaries.setdefault(cid, _Pending())
        if kind == "mention":
            summary.direct = summary.direct or summary.count > 0
            return
        if kind not in ("message", "sent"):
            return
        record = hub.store.get(cid, hub.store.head(cid))
        if record is not None and not self._own(record):
            summary.add(record)

    def _own(self, message: dict) -> bool:
        return bool(message.get("self") and message.get("by") == self.subscriber)

    async def _on_start(self) -> None:
        access.check(self)
        if self.hub is None:
            self.hub = await hub_registry.acquire(self.bot)
            self._owns_hub = True
            self._prepare()
        self._started = time.time()
        if self.debug_raw_events:
            self.hub.capture_raw(self.debug_raw_events)
        self._http = httpx.AsyncClient()

    async def _on_stop(self) -> None:
        hub, self.hub = self.hub, None
        if hub is not None:
            hub.unsubscribe(self._wakeup)
            hub.remove_listener(self._on_hub_change)
            if self._owns_hub:
                await hub_registry.release(hub)
                self._owns_hub = False
        for task in list(self._tasks):
            task.cancel()
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    # ── selection ──────────────────────────────────────────────────

    def _filter_key(self) -> str:
        if isinstance(self.conversations, str):
            return self.conversations
        return ",".join(sorted(self.conversations))

    def matches(self, cid: str) -> bool:
        """Return whether conversation ``cid`` belongs to this feed."""
        spec = self.conversations
        kind = cid.partition(":")[0]
        if isinstance(spec, str):
            match spec:
                case "all":
                    return True
                case "c2c" | "private":
                    return kind == "c2c"
                case "groups" | "group":
                    return kind == "group"
            spec = [spec]
        if cid in spec:
            return True
        handle = self.hub.store.handle(cid) if self.hub is not None else ""
        return handle in spec

    def known(self, cid: str) -> bool:
        """Return whether this feed has shown chat from ``cid``."""
        hub = self.hub
        return bool(
            hub is not None
            and self.subscriber
            and hub.store.cursor(self.subscriber, cid) is not None
        )

    def pending(self, cid: str) -> list[dict]:
        """Return unseen messages of ``cid``, excluding this feed's own sends."""
        hub = self.hub
        if hub is None or not self.subscriber:
            return []
        cursor = hub.store.cursor(self.subscriber, cid) or 0
        return [m for m in hub.store.range(cid, cursor) if not self._own(m)]

    def unread(self, cid: str) -> int:
        """Return the number of unseen messages of ``cid`` without reading the log."""
        summary = self._summaries.get(cid)
        return summary.count if summary is not None else 0

    def take(self, cid: str) -> list[dict]:
        """Return the unseen messages of ``cid`` and mark them seen."""
        hub = self.hub
        assert hub is not None and self.subscriber
        messages = self.pending(cid)
        hub.store.set_cursor(self.subscriber, cid, hub.store.head(cid))
        self._summaries[cid] = _Pending()
        return messages

    # ── delivery ───────────────────────────────────────────────────

    def _ready(self, now: float) -> tuple[list[str], float]:
        hub = self.hub
        assert hub is not None
        ready: list[str] = []
        soonest = IDLE_POLL_S
        for cid, summary in self._summaries.items():
            if summary.count == 0:
                continue
            decision = self.policy.decide_summary(
                summary.count,
                summary.direct,
                summary.newest,
                now,
                self._last_ambient.get(cid),
            )
            match decision.action:
                case "wake":
                    ready.append(cid)
                case "wait":
                    soonest = min(soonest, max(decision.delay, 0.05))
        return ready, soonest

    async def wait_for_trigger(self) -> TriggerEvent | None:
        """Return the next delivery, or None after an idle poll."""
        if not self._running or self.hub is None:
            await asyncio.sleep(IDLE_POLL_S)
            return None
        self._wakeup.clear()
        await self.hub.refresh_credentials()
        ready, soonest = self._ready(time.time())
        if not ready:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=soonest)
            except asyncio.TimeoutError:
                pass
            return None
        if self.batch_delay > 0:
            await asyncio.sleep(self.batch_delay)
        return await self.compose(ready)

    async def compose(self, cids: list[str]) -> TriggerEvent | None:
        """Drain ``cids`` into one event."""
        hub = self.hub
        assert hub is not None and self.subscriber
        blocks: list[str] = []
        shown: list[dict] = []
        delivered: list[str] = []
        direct = False
        now = time.time()
        for cid in cids:
            first_time = not self.known(cid)
            messages = self.take(cid)
            if not messages:
                continue
            meta = hub.store.conv_meta(cid) or {}
            attrs = chat_attrs(meta.get("handle", cid), meta.get("kind", ""))
            old: list[dict] = []
            if first_time and self.unseen_history == "backfill":
                old = [m for m in messages if (m.get("recv") or 0) < self._started]
                messages = messages[len(old) :]
            new_lines = self._capped(messages, self.max_delivery)
            room = self.max_delivery - min(len(messages), self.max_delivery)
            if old:
                old_lines = self._capped(old, min(self.backfill, room))
                if old_lines:
                    note = {**attrs, "note": "earlier history, already happened"}
                    blocks.append(render_block("qq_backfill", note, old_lines))
            if new_lines:
                blocks.append(render_block("qq_update", attrs, new_lines))
            everything = old + messages
            chat_direct = any(is_direct(m) for m in everything)
            direct = direct or chat_direct
            if not chat_direct:
                self._last_ambient[cid] = now
            self.hold_bypass.discard(cid)
            shown.extend(m for m in messages[-self.max_delivery :] if not m.get("self"))
            delivered.append(cid)
            if self.typing and meta.get("kind") == "c2c":
                task = asyncio.create_task(hub.typing(cid))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        if not blocks:
            return None
        self.last_delivered = delivered
        images = []
        if self._http is not None:
            images = await images_for(
                self._http, shown, self.image_options, cache_app=hub.app_id
            )
        return TriggerEvent(
            type="user_input",
            content=combine("\n\n".join(blocks), images),
            context={
                "source": "qq",
                "chats": [hub.store.handle(c) for c in delivered],
                "mention": direct,
                "message_count": len(shown),
                "image_count": len(images),
            },
            stackable=True,
        )

    def _capped(self, messages: list[dict], limit: int) -> list[str]:
        """Render the newest ``limit`` messages, noting how many were left out."""
        if limit <= 0:
            return [omitted_note(len(messages))] if messages else []
        shown = messages[-limit:]
        lines = [self._line(m) for m in shown]
        if len(messages) > len(shown):
            lines.insert(0, omitted_note(len(messages) - len(shown)))
        return lines

    def _line(self, message: dict) -> str:
        quoted = None
        if message.get("reply_seq") and self.hub is not None:
            quoted = self.hub.store.get(message["conv"], int(message["reply_seq"]))
        return render_line(message, quoted)

    def render_held(
        self, cid: str, messages: list[dict], name: str = "qq_update"
    ) -> str:
        """Render messages outside a delivery (held sends, history) as one block."""
        hub = self.hub
        meta = (hub.store.conv_meta(cid) if hub else None) or {}
        attrs = chat_attrs(meta.get("handle", cid), meta.get("kind", ""))
        return render_block(name, attrs, self._capped(messages, self.max_delivery))


def feeds_of(agent: Any) -> list[QQFeedTrigger]:
    """Return the running ``qq_feed`` triggers of an agent."""
    manager = getattr(agent, "trigger_manager", None)
    if manager is None:
        return []
    triggers = (manager.get_trigger(info.trigger_id) for info in manager.list())
    return [t for t in triggers if isinstance(t, QQFeedTrigger) and t.hub is not None]
