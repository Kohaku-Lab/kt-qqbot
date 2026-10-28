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
    ``ambient`` wakes on direct messages at once; otherwise once ``every_n``
    messages are pending, or the chat has been quiet for ``quiet_period``
    seconds, but never sooner than ``cooldown`` after the last ambient wake.
    """

    mode: WakeMode = "ambient"
    quiet_period: float = 20.0
    every_n: int = 8
    cooldown: float = 60.0

    def decide(
        self, pending: list[dict], now: float, last_ambient: float | None
    ) -> WakeDecision:
        """Return the decision for one conversation's pending messages."""
        if not pending:
            return WakeDecision("idle")
        newest = max(float(m.get("recv") or m.get("ts") or now) for m in pending)
        return self.decide_summary(
            len(pending), any(is_direct(m) for m in pending), newest, now, last_ambient
        )

    def decide_summary(
        self,
        count: int,
        direct: bool,
        newest: float,
        now: float,
        last_ambient: float | None,
    ) -> WakeDecision:
        """Return the decision from a pending count, direct flag and newest arrival."""
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
        if count >= self.every_n:
            return WakeDecision("wake")
        quiet_for = now - newest
        if quiet_for >= self.quiet_period:
            return WakeDecision("wake")
        return WakeDecision("wait", delay=self.quiet_period - quiet_for)
