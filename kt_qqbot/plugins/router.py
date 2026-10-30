"""``qq_router`` plugin: give each QQ conversation its own creature.

When a conversation's first message arrives (and for every stored
conversation at start), the router adds a creature built from
``template`` to the live engine. Its ``qq_feed`` trigger is narrowed to
that one conversation. The creature id is stable, so when its autosession
file already exists the router adopts that session instead, which brings
back its conversation.
"""

import asyncio
import weakref
from pathlib import Path
from typing import Any

from kohakuterrarium.core.config.load import load_agent_config
from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.terrarium.channels.channels import TERRARIUM_ENGINE_KEY
from kohakuterrarium.terrarium.session.autosession import default_session_dir
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot import access
from kt_qqbot import hub as hub_registry
from kt_qqbot.hub import QQHub
from kt_qqbot.records import split_conv_id

logger = get_logger("kt_qqbot.router")

FEED_TRIGGER_NAMES = {"qq_feed", "QQFeedTrigger"}


def creature_id_for(app_id: str, cid: str) -> str:
    """Return the stable, file-name-safe creature id of a conversation."""
    kind, openid = split_conv_id(cid)
    return f"qq-{app_id}-{kind}-{openid}"


def _is_feed(trigger_config: Any) -> bool:
    return (
        trigger_config.type in FEED_TRIGGER_NAMES
        or trigger_config.class_name in FEED_TRIGGER_NAMES
    )


class QQRouterPlugin(BasePlugin):
    """Spawn one creature per QQ conversation from a template creature."""

    name = "qq_router"
    priority = 50
    description = "Add one creature per QQ chat to the running engine."

    def __init__(
        self,
        template: str = "@kt-qqbot/creatures/qq-group-chat",
        bot: str | None = None,
        conversations: str = "all",
        pwd: str | None = None,
    ):
        super().__init__()
        self.template = template
        self.bot = bot
        self.conversations = conversations
        self.pwd = pwd
        self.spawned: dict[str, str] = {}
        self._ctx: PluginContext | None = None
        self._hub: QQHub | None = None
        self._owned = False
        self._spawning: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context

    def _engine(self) -> Any:
        agent = self._ctx.host_agent if self._ctx is not None else None
        env = getattr(agent, "environment", None)
        ref = env.get(TERRARIUM_ENGINE_KEY) if env is not None else None
        return ref() if isinstance(ref, weakref.ref) else ref

    def wants(self, cid: str) -> bool:
        """Return whether this router handles conversation ``cid``."""
        kind = split_conv_id(cid)[0]
        match self.conversations:
            case "c2c" | "private":
                return kind == "c2c"
            case "groups" | "group":
                return kind == "group"
        return True

    async def on_agent_start(self) -> None:
        if not access.allowed(self._ctx):
            logger.error("qq_router not started", reason=access.ADMIN_ONLY)
            return
        if self._engine() is None:
            logger.error("qq_router needs a creature running inside a Terrarium engine")
            return
        await self.attach(await hub_registry.acquire(self.bot), owned=True)

    async def attach(self, hub: QQHub, *, owned: bool = False) -> None:
        """Bind to a hub; spawn creatures for stored and new chats in the background."""
        self._hub = hub
        self._owned = owned
        hub.on_new_conversation(self._on_new)
        for cid in hub.store.conversations():
            self._on_new(cid)

    async def on_agent_stop(self) -> None:
        hub, self._hub = self._hub, None
        if hub is None:
            return
        hub.remove_new_conversation(self._on_new)
        for task in list(self._tasks):
            task.cancel()
        if self._owned:
            self._owned = False
            await hub_registry.release(hub)

    async def wait_idle(self) -> None:
        """Return once every background spawn started so far has finished."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _on_new(self, cid: str) -> None:
        task = asyncio.create_task(self.spawn(cid))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def build_config(self, cid: str, creature_id: str) -> Any:
        """Load the template and narrow its feed to one conversation."""
        config = load_agent_config(self.template)
        feeds = [t for t in config.triggers if _is_feed(t)]
        if not feeds:
            raise ValueError(f"template {self.template!r} has no qq_feed trigger")
        for trigger in feeds:
            trigger.options["conversations"] = [cid]
            trigger.options["subscriber"] = creature_id
            trigger.options["unseen_history"] = "new"
            if self.bot:
                trigger.options["bot"] = self.bot
        handle = self._hub.store.handle(cid) if self._hub is not None else cid
        config.name = f"qq-{handle}"
        return config

    async def spawn(self, cid: str) -> str | None:
        """Add the creature of ``cid`` unless it already runs; return its id."""
        engine = self._engine()
        hub = self._hub
        if engine is None or hub is None or not self.wants(cid):
            return None
        agent = self._ctx.host_agent if self._ctx is not None else None
        if not access.allowed(agent):
            logger.error(
                "qq_router refused to spawn", conv=cid, reason=access.ADMIN_ONLY
            )
            return None
        creature_id = creature_id_for(hub.app_id, cid)
        if creature_id in engine or creature_id in self._spawning:
            return creature_id
        self._spawning.add(creature_id)
        try:
            saved = default_session_dir(engine) / f"{creature_id}.kohakutr"
            if saved.exists():
                await engine.adopt_session(str(saved), pwd=self.pwd)
            else:
                config = self.build_config(cid, creature_id)
                await engine.add_creature(
                    config, creature_id=creature_id, pwd=self.pwd or str(Path.cwd())
                )
        except Exception as exc:
            logger.error(
                "qq_router could not add creature",
                conv=cid,
                creature_id=creature_id,
                error=str(exc),
                exc_info=True,
            )
            return None
        finally:
            self._spawning.discard(creature_id)
        self.spawned[cid] = creature_id
        logger.info("qq_router added creature", conv=cid, creature_id=creature_id)
        return creature_id
