"""kt-qqbot against the core hooks: sandbox path_arguments (H1),
trigger bind_host (H2) and host_resources_allowed (H3)."""

import asyncio
from pathlib import Path

import pytest

import kohakuterrarium.bootstrap.agent_init as agent_init_mod
import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod
from kohakuterrarium.builtins.plugins.sandbox.plugin import SandboxPlugin
from kohakuterrarium.modules.plugin.base import PluginBlockError, PluginContext
from kohakuterrarium.modules.tool.base import ToolContext
from kohakuterrarium.terrarium import Terrarium
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from kt_qqbot import access
from kt_qqbot import hub as hub_registry
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.plugins.router import QQRouterPlugin
from kt_qqbot.records import EVENT_C2C
from kt_qqbot.tools import QQHistoryTool, QQSendTool, QQWatchTool
from kt_qqbot.trigger import QQFeedTrigger, feeds_of

from tests.conftest import APP_ID, SECRET, c2c_event

pytestmark = pytest.mark.timeout(60)

FEED = (
    "triggers:\n"
    "  - name: qq_feed\n"
    "    type: package\n"
    "    module: kt_qqbot.trigger\n"
    "    class: QQFeedTrigger\n"
    "    wake: all\n"
    "    batch_delay: 0\n"
)
TOOLS = (
    "tools:\n"
    "  - name: qq_send\n"
    "    type: package\n"
    "    module: kt_qqbot.tools\n"
    "    class: QQSendTool\n"
    "  - name: qq_history\n"
    "    type: package\n"
    "    module: kt_qqbot.tools\n"
    "    class: QQHistoryTool\n"
)


def _creature(root: Path, name: str, *, feed: bool, extra: str = "") -> str:
    folder = root / name
    folder.mkdir()
    (folder / "config.yaml").write_text(
        f"name: {name}\n"
        "system_prompt: 'qq test'\n"
        "controller:\n  tool_format: bracket\n"
        "input:\n  type: none\n"
        "output:\n  type: stdout\n" + (FEED if feed else "") + TOOLS + extra,
        encoding="utf-8",
    )
    return str(folder)


async def _until(predicate, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


@pytest.fixture
def engine_env(monkeypatch, make_hub):
    CredentialStore().save(Credentials(APP_ID, SECRET))
    shared = make_hub()
    monkeypatch.setattr(hub_registry, "HUB_FACTORY", lambda creds: shared)
    llms: list[ScriptedLLM] = []
    script = [
        ScriptEntry(
            response="[/qq_send]\n@@message=look\n@@media=private/x.png\n[qq_send/]",
            match="send secret",
        ),
        ScriptEntry(response="(quiet)"),
    ]

    def create(config, *args, **kwargs):
        llms.append(ScriptedLLM(script))
        return llms[-1]

    monkeypatch.setattr(bootstrap_llm_mod, "create_llm_provider", create)
    monkeypatch.setattr(agent_init_mod, "create_llm_provider", create)
    return shared, llms


def test_h1_sandbox_blocks_declared_media_paths(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "x.png").write_bytes(b"png")
    (tmp_path / "ok.png").write_bytes(b"png")
    sandbox = SandboxPlugin(options={"fs_deny": [str(private)], "fs_read": "workspace"})
    ctx = ToolContext(agent_name="a", session=None, working_dir=tmp_path)

    async def check(media):
        return await sandbox.pre_tool_execute(
            {"message": "x", "media": media},
            tool_name="qq_send",
            tool=QQSendTool(),
            context=ctx,
        )

    assert asyncio.run(check("ok.png")) is None
    assert asyncio.run(check(["ok.png"])) is None
    for media in ("private/x.png", ["ok.png", "private/x.png"], "/etc/hosts"):
        with pytest.raises(PluginBlockError, match="SandboxViolation"):
            asyncio.run(check(media))


async def test_h1_h2_through_real_creatures(engine_env, tmp_path, fake_api):
    hub, llms = engine_env
    extra = (
        "plugins:\n"
        "  - name: sandbox\n"
        "    options:\n"
        f"      fs_deny: [{tmp_path / 'private'}]\n"
    )
    config = _creature(tmp_path, "twin", feed=True, extra=extra)
    engine = Terrarium()
    first = await engine.add_creature(config, pwd=str(tmp_path))
    second = await engine.add_creature(config, pwd=str(tmp_path))
    feed_a = feeds_of(first.agent)[0]
    feed_b = feeds_of(second.agent)[0]
    assert feed_a.subscriber == f"creature:{first.creature_id}"
    assert feed_b.subscriber == f"creature:{second.creature_id}"
    assert feed_a.subscriber != feed_b.subscriber

    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "x.png").write_bytes(b"png")
    await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "send secret"))
    await _until(lambda: feed_a.known("c2c:U1") and feed_b.known("c2c:U1"))
    assert hub.store.cursor(feed_a.subscriber, "c2c:U1") >= 1
    assert hub.store.cursor(feed_b.subscriber, "c2c:U1") >= 1
    await _until(lambda: all(len(llm.call_log) >= 2 for llm in llms))
    tool_feedback = " ".join(str(m) for m in llms[0].call_log[1])
    assert "SandboxViolation" in tool_feedback
    assert not fake_api.sent("/files"), "the sandbox stopped the upload"
    await engine.shutdown()


async def test_h3_non_admin_engine_refuses_admin_engine_allows(engine_env, tmp_path):
    hub, _ = engine_env
    feed_config = _creature(tmp_path, "with-feed", feed=True)
    tools_config = _creature(tmp_path, "tools-only", feed=False)

    denied = Terrarium(host_resources=False)
    with pytest.raises(access.AdminCreaturesOnly):
        await denied.add_creature(feed_config)
    creature = await denied.add_creature(tools_config)
    ctx = ToolContext(
        agent_name="tools-only",
        session=None,
        working_dir=tmp_path,
        agent=creature.agent,
        environment=creature.agent.environment,
        creature_id=creature.creature_id,
    )
    for tool, args in (
        (QQSendTool(), {"message": "x"}),
        (QQHistoryTool(), {}),
        (QQWatchTool(), {}),
    ):
        result = await tool.execute(args, context=ctx)
        assert result.error == access.ADMIN_ONLY, tool.tool_name
    router = QQRouterPlugin()
    plugin_ctx = PluginContext(agent_name="tools-only", _host_agent=creature.agent)
    await router.on_load(plugin_ctx)
    await router.on_agent_start()
    assert router._hub is None, "the router does not start"
    router._hub = hub
    assert await router.spawn("c2c:U1") is None
    unbound = QQFeedTrigger()
    unbound.bind_host(creature.creature_id, "tools-only", creature.agent)
    with pytest.raises(access.AdminCreaturesOnly):
        await unbound.start()
    await denied.shutdown()

    allowed = Terrarium(host_resources=True)
    fed = await allowed.add_creature(feed_config)
    feed = feeds_of(fed.agent)[0]
    assert feed.is_running and access.allowed(feed)
    ctx_ok = ToolContext(
        agent_name="with-feed",
        session=None,
        working_dir=tmp_path,
        agent=fed.agent,
        environment=fed.agent.environment,
        creature_id=fed.creature_id,
    )
    history = await QQHistoryTool().execute({}, context=ctx_ok)
    assert history.error != access.ADMIN_ONLY
    await allowed.shutdown()
