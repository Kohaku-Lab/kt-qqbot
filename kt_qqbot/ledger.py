"""Passive-reply bookkeeping: which inbound message id a reply can ride on.

With a :class:`~kt_qqbot.store.ChatStore`, slots are kept in the bot's vault
so a restarted hub neither reuses a spent ``msg_id`` nor repeats a
``msg_seq``.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kt_qqbot.store import ChatStore

MAX_REPLIES = {"group": 5, "c2c": 4}
WINDOW_S = {"group": 300, "c2c": 3600}
KEEP_PER_CONV = 20


@dataclass
class _Slot:
    msg_id: str
    received_at: float
    uses: int = 0


@dataclass(frozen=True)
class ReplyTicket:
    """The ``msg_id`` / ``msg_seq`` pair for one passive reply."""

    msg_id: str
    msg_seq: int


class ReplyLedger:
    """Track reply budgets of recent inbound messages per conversation."""

    def __init__(
        self,
        *,
        max_replies: dict[str, int] | None = None,
        windows: dict[str, float] | None = None,
        clock: Callable[[], float] = time.time,
        store: "ChatStore | None" = None,
    ):
        self.max_replies = {**MAX_REPLIES, **(max_replies or {})}
        self.windows = {**WINDOW_S, **(windows or {})}
        self._clock = clock
        self._store = store
        self._slots: dict[str, deque[_Slot]] = {}
        if store is not None:
            self._load()

    def _window(self, cid: str) -> float:
        kind = cid.partition(":")[0]
        return self.windows.get(kind, WINDOW_S["group"])

    def limit(self, kind: str) -> int:
        """Return how many replies one inbound message of ``kind`` allows."""
        return self.max_replies.get(kind, MAX_REPLIES["group"])

    def _load(self) -> None:
        stored = sorted(
            self._store.slots().items(), key=lambda item: item[1]["received_at"]
        )
        for msg_id, data in stored:
            slots = self._slots.setdefault(data["conv"], deque())
            slots.append(_Slot(msg_id, float(data["received_at"]), int(data["used"])))
        self.prune()

    def _save(self, cid: str, slot: _Slot) -> None:
        if self._store is not None:
            self._store.save_slot(
                slot.msg_id,
                {
                    "conv": cid,
                    "used": slot.uses,
                    "last_seq": slot.uses,
                    "received_at": slot.received_at,
                },
            )

    def _drop(self, slot: _Slot) -> None:
        if self._store is not None:
            self._store.delete_slot(slot.msg_id)

    def prune(self) -> None:
        """Forget slots outside their conversation's reply window."""
        now = self._clock()
        for cid, slots in list(self._slots.items()):
            window = self._window(cid)
            while slots and now - slots[0].received_at >= window:
                self._drop(slots.popleft())
            if not slots:
                del self._slots[cid]

    def note_inbound(self, cid: str, msg_id: str, at: float | None = None) -> None:
        """Record an inbound message that later replies may reference."""
        slots = self._slots.setdefault(cid, deque())
        slot = _Slot(msg_id, self._clock() if at is None else at)
        index = len(slots)
        while index > 0 and slots[index - 1].received_at > slot.received_at:
            index -= 1
        slots.insert(index, slot)
        self._save(cid, slot)
        while len(slots) > KEEP_PER_CONV:
            self._drop(slots.popleft())
        self.prune()

    def take(self, cid: str, kind: str, *, reserve: int = 0) -> ReplyTicket | None:
        """Reserve one reply on the newest usable message; None when the window is closed.

        ``reserve`` keeps that many replies of the slot free for later sends.
        """
        window = self.windows.get(kind, WINDOW_S["group"])
        limit = self.limit(kind) - reserve
        now = self._clock()
        for slot in reversed(self._slots.get(cid, ())):
            if now - slot.received_at >= window:
                break
            if slot.uses < limit:
                slot.uses += 1
                self._save(cid, slot)
                return ReplyTicket(slot.msg_id, slot.uses)
        self.prune()
        return None

    def exhaust(self, cid: str, kind: str, msg_id: str) -> None:
        """Mark a slot used up (QQ said its window or budget is gone)."""
        for slot in self._slots.get(cid, ()):
            if slot.msg_id == msg_id:
                slot.uses = self.limit(kind)
                self._save(cid, slot)

    def usable(self, cid: str, kind: str) -> bool:
        """Return whether a passive reply is possible right now."""
        window = self.windows.get(kind, WINDOW_S["group"])
        now = self._clock()
        return any(
            now - s.received_at < window and s.uses < self.limit(kind)
            for s in self._slots.get(cid, ())
        )

    def free_slots(self, cid: str, kind: str) -> int:
        """Return how many passive replies the conversation can still send now."""
        window = self.windows.get(kind, WINDOW_S["group"])
        now = self._clock()
        limit = self.limit(kind)
        return sum(
            limit - s.uses
            for s in self._slots.get(cid, ())
            if now - s.received_at < window and s.uses < limit
        )
