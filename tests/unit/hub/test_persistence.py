"""Bot-side state that must survive a process restart."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.server import serve

import kohakuterrarium.bootstrap.agent_init as agent_init_mod
import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod
from kohakuterrarium.terrarium import Terrarium
from kohakuterrarium.testing.llm import ScriptedLLM

from kt_qqbot import hub as hub_registry
from kt_qqbot.api import QQApi
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.gateway import Gateway
from kt_qqbot.hub import QQHub
from kt_qqbot.plugins.router import QQRouterPlugin, creature_id_for
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.store import ChatStore
from kt_qqbot.trigger import feeds_of

from tests.conftest import APP_ID, SECRET, _NoGateway, c2c_event, group_event

pytestmark = pytest.mark.timeout(60)
BOT = {"id": "BOT9", "username": "kohaku-bot", "bot": True}


class _Tokens:
    async def get(self):
        return "tok"

    def invalidate(self):
        pass


class _WsApi:
    def __init__(self, url):
        self.url = url
        self.tokens = _Tokens()

    async def gateway_url(self):
        return self.url

    async def aclose(self):
        pass


async def _until(predicate, timeout=10.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def _frame(seq, data, kind=EVENT_C2C):
    return json.dumps({"op": 0, "s": seq, "t": kind, "d": data})


async def test_gateway_session_and_identity_survive_restart(tmp_path):
    store_path = tmp_path / "bot.kvault"
    seen: list[dict] = []
    sessions = []

    async def first(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        seen.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps(
                {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1", "user": BOT}}
            )
        )
        await ws.send(_frame(2, c2c_event("A", "U1", "before restart")))
        await ws.wait_closed()

    async def resumed(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        seen.append(json.loads(await ws.recv()))
        await ws.send(_frame(2, c2c_event("A", "U1", "before restart")))
        await ws.send(_frame(3, c2c_event("B", "U1", "missed while down")))
        await ws.send(json.dumps({"op": 0, "s": 4, "t": "RESUMED", "d": {}}))
        await ws.wait_closed()

    async def invalid(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"op": 9, "d": False}))
        await asyncio.sleep(0.2)

    async def identify_again(ws):
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        seen.append(json.loads(await ws.recv()))
        await ws.send(
            json.dumps(
                {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S2", "user": BOT}}
            )
        )
        await ws.wait_closed()

    script = [first, resumed, invalid, identify_again]

    async def handler(ws):
        sessions.append(1)
        await script[len(sessions) - 1](ws)

    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"

        def make():
            return QQHub(
                Credentials(APP_ID, SECRET),
                api=_WsApi(url),
                store=ChatStore(store_path),
                gateway_factory=lambda api, cb: Gateway(api, cb, sleep=_fast),
            )

        hub = make()
        hub.start()
        await _until(lambda: hub.store.head("c2c:U1") == 1)
        assert hub.bot_identity["username"] == "kohaku-bot"
        await hub.close()

        hub = make()
        assert hub.bot_id == "BOT9", "identity is known before READY"
        assert hub.bot_identity["username"] == "kohaku-bot"
        hub.start()
        await _until(lambda: hub.store.head("c2c:U1") == 2)
        await asyncio.sleep(0.1)
        assert [m["content"] for m in hub.store.range("c2c:U1", 0)] == [
            "before restart",
            "missed while down",
        ], "replayed events land once"
        await hub.close()

        hub = make()
        hub.start()
        await _until(lambda: len(seen) == 4)
        await _until(lambda: hub.gateway.session_id == "S2")
        await hub.close()

    assert seen[0]["op"] == 2
    assert seen[1] == {
        "op": 6,
        "d": {"token": "QQBot tok", "session_id": "S1", "seq": 2},
    }
    assert seen[2]["op"] == 6 and seen[2]["d"]["seq"] == 4
    assert seen[3]["op"] == 2, "an invalid session falls back to IDENTIFY"
    reopened = ChatStore(store_path)
    assert reopened.get_state("gateway")["session_id"] == "S2"
    reopened.close()


async def _fast(delay):
    await asyncio.sleep(0.01)


def test_chat_metadata_is_restored_and_bounded(tmp_path):
    path = tmp_path / "s.kvault"
    store = ChatStore(path, max_aliases=3)
    for i in range(5):
        store.append(
            {
                "conv": "group:G",
                "kind": "group",
                "msg_id": f"m{i}",
                "author": f"MEMBER{i}",
                "name": "Kohaku" if i == 0 else "",
                "ts": 100.0 + i,
            }
        )
    meta = store.conv_meta("group:G")
    first_seen = meta["first_seen"]
    store.close()
    store = ChatStore(path, max_aliases=3)
    meta = store.conv_meta("group:G")
    assert meta["kind"] == "group" and meta["handle"] == "g_g"
    assert meta["first_seen"] == first_seen and meta["last_seen"] >= first_seen
    assert store.alias("group:G", "MEMBER0") == "Kohaku"
    assert store.alias("group:G", "MEMBER1") == "#memb"
    assert meta["alias_count"] == 3, "stored aliases stay bounded"
    assert store.get("group:G", 5)["alias"].startswith("#member")
    store.close()


def _llm(monkeypatch, replies):
    made: dict[str, list[ScriptedLLM]] = {}

    def create(config, *args, **kwargs):
        llm = ScriptedLLM(list(replies))
        made.setdefault(getattr(config, "name", ""), []).append(llm)
        return llm

    monkeypatch.setattr(bootstrap_llm_mod, "create_llm_provider", create)
    monkeypatch.setattr(agent_init_mod, "create_llm_provider", create)
    return made


def _factory(tmp_path, fake_api):
    def make(creds):
        api = QQApi(
            creds,
            httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler)),
            api_base="https://api.test",
            token_url="https://api.test/app/getAppAccessToken",
        )
        return QQHub(
            creds,
            api=api,
            store=ChatStore(tmp_path / "chat.kvault"),
            gateway_factory=_NoGateway,
        )

    return make


def _config(root: Path) -> Path:
    folder = root / "bot"
    folder.mkdir()
    (folder / "config.yaml").write_text(
        "name: qq-solo\n"
        "system_prompt: 'qq'\n"
        "controller:\n  tool_format: bracket\n"
        "input:\n  type: none\n"
        "output:\n  type: stdout\n"
        "triggers:\n"
        "  - name: qq_feed\n"
        "    type: package\n"
        "    module: kt_qqbot.trigger\n"
        "    class: QQFeedTrigger\n"
        "    wake: all\n"
        "    batch_delay: 0\n",
        encoding="utf-8",
    )
    return folder


def _user_texts(agent) -> list[str]:
    return [
        str(m.content)
        for m in agent.controller.conversation.get_messages()
        if m.role == "user"
    ]


async def test_single_creature_resumes_with_history_and_one_feed(
    tmp_path, monkeypatch, fake_api
):
    CredentialStore().save(Credentials(APP_ID, SECRET))
    monkeypatch.setattr(hub_registry, "HUB_FACTORY", _factory(tmp_path, fake_api))
    _llm(monkeypatch, ["noted"])
    session_dir = tmp_path / "sessions"
    engine = Terrarium(session_dir=str(session_dir))
    creature = await engine.add_creature(str(_config(tmp_path)), pwd=str(tmp_path))
    hub = hub_registry.running_hubs()[APP_ID]
    await hub.on_dispatch(EVENT_C2C, c2c_event("A", "U1", "first turn"))
    await _until(lambda: len(_user_texts(creature.agent)) == 1)
    saved = session_dir / f"{creature.creature_id}.kohakutr"
    await engine.shutdown()
    assert hub_registry.running_hubs() == {}

    resumed_engine = await Terrarium.resume(str(saved))
    resumed = next(iter(resumed_engine._creatures.values()))
    feeds = feeds_of(resumed.agent)
    assert len(feeds) == 1 and len(resumed.agent.trigger_manager.list()) == 1
    hub = hub_registry.running_hubs()[APP_ID]
    await _until(lambda: hub._refs == 1)
    assert hub._refs == 1
    assert "first turn" in _user_texts(resumed.agent)[0]
    await hub.on_dispatch(EVENT_C2C, c2c_event("B", "U1", "after resume"))
    await _until(lambda: len(_user_texts(resumed.agent)) == 2)
    await asyncio.sleep(0.2)
    texts = _user_texts(resumed.agent)
    assert len(texts) == 2, "no duplicate delivery"
    assert "after resume" in texts[1] and "first turn" not in texts[1]
    await resumed_engine.shutdown()


async def test_router_reattaches_each_chat_session(tmp_path, monkeypatch, fake_api):
    CredentialStore().save(Credentials(APP_ID, SECRET))
    monkeypatch.setattr(hub_registry, "HUB_FACTORY", _factory(tmp_path, fake_api))
    _llm(monkeypatch, ["ok"])
    template = _config(tmp_path)
    session_dir = tmp_path / "sessions"
    router_dir = tmp_path / "router"
    router_dir.mkdir()
    (router_dir / "config.yaml").write_text(
        "name: router\nsystem_prompt: 'r'\ninput:\n  type: none\n"
        "output:\n  type: stdout\n",
        encoding="utf-8",
    )

    async def run_router():
        engine = Terrarium(session_dir=str(session_dir))
        host = await engine.add_creature(str(router_dir), pwd=str(tmp_path))
        router = QQRouterPlugin(template=str(template), pwd=str(tmp_path))
        await router.on_load(_PluginCtx(host.agent))
        await router.on_agent_start()
        return engine, router

    engine, router = await run_router()
    hub = hub_registry.running_hubs()[APP_ID]
    await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G1", "P", "remember me"))
    cid = creature_id_for(APP_ID, "group:G1")
    await _until(lambda: cid in engine)
    chat = engine.get_creature(cid)
    await _until(lambda: len(_user_texts(chat.agent)) == 1)
    await router.on_agent_stop()
    await engine.shutdown()

    engine, router = await run_router()
    await _until(lambda: cid in engine)
    chat = engine.get_creature(cid)
    await _until(lambda: len(_user_texts(chat.agent)) >= 1)
    assert "remember me" in _user_texts(chat.agent)[0], "history came back"
    await _until(lambda: len(feeds_of(chat.agent)) == 1)
    assert len(feeds_of(chat.agent)) == 1
    hub = hub_registry.running_hubs()[APP_ID]
    await _until(lambda: hub._refs == 2)
    assert hub._refs == 2, "router + one chat creature"
    await hub.on_dispatch(EVENT_GROUP, group_event("G2", "G1", "P", "new one"))
    await _until(lambda: len(_user_texts(chat.agent)) == 2)
    await asyncio.sleep(0.2)
    assert len(_user_texts(chat.agent)) == 2
    await router.on_agent_stop()
    await engine.shutdown()


class _PluginCtx:
    def __init__(self, agent):
        self.host_agent = agent
        self.environment = agent.environment
