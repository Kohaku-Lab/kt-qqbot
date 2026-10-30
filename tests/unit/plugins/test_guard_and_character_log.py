"""qq_guard keeps graph/drive tools away from QQ creatures; character load logging."""

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

import kohakuterrarium.bootstrap.agent_init as agent_init_mod
import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod
from kohakuterrarium.modules.plugin.base import PluginBlockError
from kohakuterrarium.terrarium import Terrarium
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from kt_qqbot import hub as hub_registry
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.paths import root_dir
from kt_qqbot.plugins.character import QQCharacterPlugin
from kt_qqbot.plugins.guard import QQGuardPlugin
from kt_qqbot.records import EVENT_C2C

from tests.conftest import APP_ID, SECRET, c2c_event

pytestmark = pytest.mark.timeout(60)

GUARD = (
    "  - name: qq_guard\n"
    "    type: package\n"
    "    module: kt_qqbot.plugins.guard\n"
    "    class: QQGuardPlugin\n"
)


def _creature(root: Path, name: str, fmt: str, guard: bool) -> str:
    folder = root / name
    folder.mkdir()
    (folder / "config.yaml").write_text(
        f"name: {name}\nsystem_prompt: 'qq'\n"
        f"controller:\n  tool_format: {fmt}\n"
        "input:\n  type: none\noutput:\n  type: stdout\n"
        "triggers:\n  - name: qq_feed\n    type: package\n"
        "    module: kt_qqbot.trigger\n    class: QQFeedTrigger\n"
        "    wake: all\n    batch_delay: 0\n" + ("plugins:\n" + GUARD if guard else ""),
        encoding="utf-8",
    )
    return str(folder)


@pytest.fixture
def engine_env(monkeypatch, make_hub):
    CredentialStore().save(Credentials(APP_ID, SECRET))
    shared = make_hub()
    monkeypatch.setattr(hub_registry, "HUB_FACTORY", lambda creds: shared)
    llms: dict[str, ScriptedLLM] = {}
    script = [
        ScriptEntry(response="[/group_status]\n[group_status/]", match="take over"),
        ScriptEntry(response="(quiet)"),
    ]

    def create(config, *args, **kwargs):
        llm = ScriptedLLM(script)
        llms[getattr(config, "name", "")] = llm
        return llm

    monkeypatch.setattr(bootstrap_llm_mod, "create_llm_provider", create)
    monkeypatch.setattr(agent_init_mod, "create_llm_provider", create)
    return shared, llms


async def test_privileged_qq_creature_cannot_see_or_run_graph_tools(
    engine_env, tmp_path
):
    hub, llms = engine_env
    engine = Terrarium()
    guarded = await engine.add_creature(
        _creature(tmp_path, "guarded", "native", True), is_privileged=True
    )
    plain = await engine.add_creature(
        _creature(tmp_path, "plain", "native", False), is_privileged=True
    )
    tools = guarded.agent.registry.list_tools()
    assert "group_add_node" in tools, "the engine did register privileged tools"
    visible = {s.name for s in guarded.agent.controller._get_native_tool_schemas()}
    assert not {n for n in visible if n.startswith(("group_", "drive_"))}
    assert "qq_send" not in tools or "qq_send" in visible
    unguarded = {s.name for s in plain.agent.controller._get_native_tool_schemas()}
    assert "group_add_node" in unguarded, "no guard, no change"

    bracket = await engine.add_creature(
        _creature(tmp_path, "bracket", "bracket", True), is_privileged=True
    )
    await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "take over the group"))
    deadline = asyncio.get_running_loop().time() + 20
    target = llms["bracket"]
    while len(target.call_log) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
    feedback = " ".join(str(m) for m in target.call_log[1])
    assert "not available to this QQ creature" in feedback
    assert bracket.creature_id
    await engine.shutdown()


async def test_guard_options():
    guard = QQGuardPlugin(allow_tools=["group_status"], blocked_tools=["bash"])
    assert await guard.pre_tool_execute({}, tool_name="group_status") is None
    for name in ("group_add_node", "drive_create", "bash"):
        with pytest.raises(PluginBlockError):
            await guard.pre_tool_execute({}, tool_name=name)
    assert await guard.pre_tool_execute({}, tool_name="qq_send") is None


def test_character_load_is_logged_once_per_change(caplog):
    caplog.set_level(logging.INFO, logger="kt_qqbot.character")
    base = root_dir()
    (base / "character.md").write_text(
        "I am Kohaku, a secret phrase.", encoding="utf-8"
    )
    plugin = QQCharacterPlugin()

    def loaded():
        return [
            r for r in caplog.records if r.getMessage().startswith("Character loaded")
        ]

    plugin.get_prompt_content(None)
    assert len(loaded()) == 1
    plugin.get_prompt_content(None)
    assert len(loaded()) == 1, "unchanged files log nothing"
    path = base / "character.md"
    path.write_text("I am Kohaku, now longer.", encoding="utf-8")
    later = time.time() + 5
    os.utime(path, (later, later))
    plugin.get_prompt_content(None)
    assert len(loaded()) == 2
    assert all(
        "secret phrase" not in r.getMessage() and "secret phrase" not in str(r.__dict__)
        for r in caplog.records
    )
