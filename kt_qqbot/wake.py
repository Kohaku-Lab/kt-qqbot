"""Decide when pending chat in one conversation should wake the creature."""

from dataclasses import dataclass
from typing import Literal

WakeMode = Literal["mention", "all", "ambient"]


@dataclass(frozen=True)
class WakeDecision:
    """``wake`` now, ``wait`` ``delay`` seconds and check again, or stay ``idle``."""

    action: Literal["wake", "wait", "idle"]
    delay: float = 0.0
    direct: bool = False


def is_direct(message: dict) -> bool:
    """Return whether a message addresses the bot: private chat, @-mention, or reply to it."""
    return bool(
        message.get("kind") == "c2c" or message.get("mention") or message.get("to_self")
    )


@dataclass(frozen=True)
class WakePolicy:
    """Wake rule for one feed.

    ``mention`` wakes only on direct messages. ``all`` wakes on any message.
    ``ambient`` wakes on direct messages at once, and flushes every pending
    message with them. Otherwise it debounces: after the newest message it
    waits ``max(quiet_period * decay ** (pending - 1), min_wait)`` seconds, and
    never longer than ``max_wait`` after the first pending message. Two ambient
    wakes in one chat are at least ``cooldown`` seconds apart.
    """

    mode: WakeMode = "ambient"
    quiet_period: float = 10.0
    decay: float = 0.5
    min_wait: float = 2.0
    max_wait: float = 30.0
    cooldown: float = 15.0

    def debounce(self, count: int) -> float:
        """Return how long the chat must stay quiet with ``count`` messages pending."""
        return max(self.quiet_period * self.decay ** max(count - 1, 0), self.min_wait)

    def decide(
        self, pending: list[dict], now: float, last_ambient: float | None
    ) -> WakeDecision:
        """Return the decision for one conversation's pending messages."""
        if not pending:
            return WakeDecision("idle")
        arrivals = [float(m.get("recv") or m.get("ts") or now) for m in pending]
        return self.decide_summary(
            len(pending),
            any(is_direct(m) for m in pending),
            min(arrivals),
            max(arrivals),
            now,
            last_ambient,
        )

    def decide_summary(
        self,
        count: int,
        direct: bool,
        first: float,
        newest: float,
        now: float,
        last_ambient: float | None,
    ) -> WakeDecision:
        """Return the decision from the pending count, direct flag and first and newest arrival."""
        if count <= 0:
            return WakeDecision("idle")
        if direct:
            return WakeDecision("wake", direct=True)
        match self.mode:
            case "mention":
                return WakeDecision("idle")
            case "all":
                return WakeDecision("wake")
        if last_ambient is not None and now - last_ambient < self.cooldown:
            return WakeDecision("wait", delay=self.cooldown - (now - last_ambient))
        due = min(newest + self.debounce(count), first + self.max_wait)
        if now >= due:
            return WakeDecision("wake")
        return WakeDecision("wait", delay=due - now)
