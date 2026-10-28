"""Persona image: selection, load log, downscale cache, pinning, and the persona handle."""

import io
import logging
import os
import time
from types import SimpleNamespace

import pytest
from PIL import Image

from kohakuterrarium.core.agent import Agent
from kohakuterrarium.core.trigger_manager import TriggerManager
from kohakuterrarium.llm.message import ImagePart, TextPart
from kohakuterrarium.modules.plugin.base import PluginContext
from kohakuterrarium.modules.tool.base import ToolContext
from kohakuterrarium.testing.llm import ScriptedLLM

from kt_qqbot.paths import root_dir
from kt_qqbot.persona import MAX_SIDE, PERSONA_NOTE, find_persona, prepare_persona
from kt_qqbot.plugins.character import QQCharacterPlugin
from kt_qqbot.records import EVENT_C2C
from kt_qqbot.tools import QQSendTool
from kt_qqbot.trigger import QQFeedTrigger

from tests.conftest import c2c_event

pytestmark = pytest.mark.timeout(30)


def _image(path, size=(2000, 1000), fmt="PNG", color=(200, 30, 30)):
    Image.new("RGB", size, color).save(path, format=fmt)
    return path


def test_selection_order_and_warning(caplog):
    caplog.set_level(logging.INFO, logger="kt_qqbot.character")
    base = root_dir()
    assert find_persona(base) == (None, [])
    _image(base / "character.webp", fmt="WEBP")
    _image(base / "character.jpg", fmt="JPEG")
    chosen, ignored = find_persona(base)
    assert chosen.name == "character.jpg" and [p.name for p in ignored] == [
        "character.webp"
    ]
    _image(base / "character.png")
    chosen, ignored = find_persona(base)
    assert chosen.name == "character.png"
    assert [p.name for p in ignored] == ["character.jpg", "character.webp"]
    plugin = QQCharacterPlugin()
    plugin.sync_persona()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "character.jpg" in str(warnings[0].__dict__)


def test_downscale_and_cache(tmp_path):
    path = _image(tmp_path / "character.png", size=(2000, 1000))
    first = prepare_persona(path, tmp_path / "cache")
    assert (first.width, first.height) == (2000, 1000)
    shrunk = Image.open(io.BytesIO(first.data))
    assert max(shrunk.size) == MAX_SIDE and shrunk.size == (768, 384)
    cached = list((tmp_path / "cache").iterdir())
    assert len(cached) == 1
    again = prepare_persona(path, tmp_path / "cache")
    assert again.data == first.data and len(list((tmp_path / "cache").iterdir())) == 1
    small = prepare_persona(
        _image(tmp_path / "small.jpg", (300, 200), "JPEG"), tmp_path / "cache"
    )
    assert Image.open(io.BytesIO(small.data)).size == (300, 200)
    assert small.mime == "image/jpeg"


class FakePins:
    def __init__(self):
        self.calls = []

    def pin_message(self, key, role, content):
        self.calls.append(("pin", key, role, content))
        return True

    def unpin_message(self, key):
        self.calls.append(("unpin", key))
        return True


async def test_pin_on_start_change_and_removal(caplog):
    caplog.set_level(logging.INFO, logger="kt_qqbot.character")
    path = _image(root_dir() / "character.png", size=(1000, 1000))
    ctx = FakePins()
    plugin = QQCharacterPlugin()
    await plugin.on_load(ctx)
    await plugin.on_agent_start()
    kind, key, role, content = ctx.calls[0]
    assert (kind, key, role) == ("pin", "qq_persona", "user")
    assert isinstance(content[0], TextPart) and content[0].text == PERSONA_NOTE
    assert isinstance(content[1], ImagePart) and content[1].detail == "low"
    assert content[1].url.startswith("data:image/png;base64,")

    def loaded():
        return [r for r in caplog.records if r.getMessage() == "Persona image loaded"]

    assert len(loaded()) == 1
    assert loaded()[0].__dict__.get("width") == 1000
    await plugin.on_event(SimpleNamespace(type="user_input"))
    assert len(ctx.calls) == 1 and len(loaded()) == 1, "unchanged: no repin, no log"

    _image(path, size=(800, 600), color=(0, 0, 255))
    later = time.time() + 5
    os.utime(path, (later, later))
    await plugin.on_event(SimpleNamespace(type="user_input"))
    assert ctx.calls[-1][0] == "pin" and len(loaded()) == 2
    path.unlink()
    await plugin.on_event(SimpleNamespace(type="user_input"))
    assert ctx.calls[-1] == ("unpin", "qq_persona")
    await plugin.on_event(SimpleNamespace(type="user_input"))
    assert ctx.calls.count(("unpin", "qq_persona")) == 1
    for record in caplog.records:
        assert "base64" not in str(record.__dict__)


async def test_unbound_context_pins_nothing():
    _image(root_dir() / "character.png", size=(100, 100))
    plugin = QQCharacterPlugin()
    await plugin.on_load(PluginContext(agent_name="x"))
    await plugin.on_agent_start()
    assert plugin._pinned is False, "pin_message returns False without an agent"
    (root_dir() / "character.png").unlink()
    await plugin.on_event(SimpleNamespace(type="user_input"))


async def test_core_pins_reach_the_request(tmp_path):
    _image(root_dir() / "character.png", size=(64, 64))
    folder = tmp_path / "c"
    folder.mkdir()
    (folder / "config.yaml").write_text(
        "name: pinned\nsystem_prompt: 'sys'\ninput:\n  type: none\n"
        "output:\n  type: stdout\nplugins:\n  - name: qq_character\n"
        "    type: package\n    module: kt_qqbot.plugins.character\n"
        "    class: QQCharacterPlugin\n",
        encoding="utf-8",
    )
    llm = ScriptedLLM(["hello"])
    agent = await Agent.build(str(folder), llm=llm)
    await agent.start()
    try:
        await agent.run("hi there")
        first = llm.call_log[0]
        assert first[0]["role"] == "system"
        pinned = first[1]
        assert pinned["role"] == "user"
        parts = pinned["content"]
        assert parts[0]["text"].startswith("[reference image]")
        assert parts[1]["type"] == "image_url"
        (root_dir() / "character.png").unlink()
        await agent.run("again")
        assert all(
            "[reference image]" not in str(m.get("content")) for m in llm.call_log[1]
        ), "removing the file unpins"
    finally:
        await agent.stop()


async def test_persona_handle(make_hub, fake_api, tmp_path):
    hub = make_hub()
    feed = QQFeedTrigger(batch_delay=0, wake="all", subscriber="me")
    await feed.attach(hub)
    await feed.start()
    await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "show me you"))
    for _ in range(3):
        if await feed.wait_for_trigger():
            break
    manager = TriggerManager(process_event=None)
    manager._triggers["f"] = feed
    ctx = ToolContext(
        agent_name="bot",
        session=None,
        working_dir=tmp_path,
        agent=SimpleNamespace(trigger_manager=manager),
    )
    missing = await QQSendTool().execute({"media": "persona"}, context=ctx)
    assert "no persona image" in missing.error
    path = _image(root_dir() / "character.png", size=(50, 50))
    ok = await QQSendTool().execute({"media": "persona"}, context=ctx)
    assert ok.success, ok.error
    upload = fake_api.sent("/files")[-1]
    assert upload["file_type"] == 1 and upload["file_data"]
    assert path.read_bytes()
    other = tmp_path / "elsewhere"
    other.mkdir()
    moved = QQSendTool(character_directory=str(other))
    none_there = await moved.execute({"media": "persona"}, context=ctx)
    assert str(other) in none_there.error
    _image(other / "character.webp", size=(40, 40), fmt="WEBP")
    assert (await moved.execute({"media": "persona"}, context=ctx)).success
    await feed.stop()
    await hub.close()
