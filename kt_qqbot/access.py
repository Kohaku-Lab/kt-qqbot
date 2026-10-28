"""Gate the QQ bot to admin creatures under multi-user mode.

Uses the core ``host_resources_allowed`` hook: True on single-user hosts, for
solo agents and for admin engines; False for non-admin engines under L4.
"""

from typing import Any

from kohakuterrarium.terrarium import host_resources_allowed

from kt_qqbot.errors import QQBotError

ADMIN_ONLY = "QQ bot is available to admin creatures only"


class AdminCreaturesOnly(QQBotError, PermissionError):
    """The creature is not allowed to use the QQ bot."""

    def __init__(self) -> None:
        super().__init__(ADMIN_ONLY)


def allowed(source: Any) -> bool:
    """Return whether ``source`` (tool/plugin context, bound trigger, agent) may use the bot."""
    return bool(host_resources_allowed(source))


def check(source: Any) -> None:
    """Raise :class:`AdminCreaturesOnly` unless ``source`` may use the bot."""
    if not allowed(source):
        raise AdminCreaturesOnly()
