"""``qq_guard`` plugin: keep graph and Drive tools away from a QQ-facing creature.

QQ users can steer the creature, and a dashboard-started creature is
privileged, so it carries ``group_*`` and ``drive_*`` tools. This plugin hides
them from the model and refuses them if called anyway.
"""

from typing import Any

from kohakuterrarium.modules.plugin.base import (
    BasePlugin,
    PluginBlockError,
    PluginContext,
    ToolVisibility,
)

BLOCKED_PREFIXES = ("group_", "drive_")


class QQGuardPlugin(BasePlugin):
    """Hide and block graph-mutation and Drive tools unless allowed."""

    name = "qq_guard"
    priority = 5
    description = "Hide and block group_* / drive_* tools from a QQ-facing creature."

    def __init__(
        self,
        allow_tools: list[str] | None = None,
        blocked_tools: list[str] | None = None,
        blocked_prefixes: list[str] | None = None,
    ):
        super().__init__()
        self.allow_tools = set(allow_tools or [])
        self.blocked_tools = set(blocked_tools or [])
        self.blocked_prefixes = tuple(
            BLOCKED_PREFIXES if blocked_prefixes is None else blocked_prefixes
        )

    def blocks(self, tool_name: str) -> bool:
        """Return whether ``tool_name`` is kept from the model."""
        if tool_name in self.allow_tools:
            return False
        return tool_name in self.blocked_tools or tool_name.startswith(
            self.blocked_prefixes
        )

    def get_tool_visibility(self, context: PluginContext) -> ToolVisibility | None:
        """Allow every registered tool except the blocked ones."""
        registry = getattr(context, "registry", None)
        if registry is None:
            return None
        names = registry.list_tools()
        return ToolVisibility(
            allowed_tools=frozenset(n for n in names if not self.blocks(n))
        )

    async def pre_tool_execute(self, args: dict, **kwargs: Any) -> dict | None:
        """Refuse a blocked tool that was called anyway."""
        tool_name = str(kwargs.get("tool_name") or "")
        if self.blocks(tool_name):
            raise PluginBlockError(f"{tool_name} is not available to this QQ creature")
        return None
