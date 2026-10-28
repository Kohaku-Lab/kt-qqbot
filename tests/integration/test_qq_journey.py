"""Whole kt-qqbot flow inside a real Terrarium engine with a scripted LLM.

One journey: credentials → router creature → first private message spawns a
per-chat creature that replies through qq_send with text, an image and a file → a group chat spawns a
second creature that stays silent → engine shutdown releases the hub →
a new engine re-spawns both chats, delivers only new messages, and
continues the persisted passive-reply slots.
"""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import kohakuterrarium.bootstrap.agent_init as agent_init_mod
import kohakuterrarium.bootstrap.llm as bootstrap_llm_mod

from kohakuterrarium.api.app import create_app
from kohakuterrarium.api.deps import set_service
from kohakuterrarium.packages.install import install_package
from kohakuterrarium.studio.identity.ui_extensions import set_enabled
from kohakuterrarium.terrarium import LocalTerrariumService, Terrarium
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from kt_qqbot import api as qq_api
from kt_qqbot import hub as hub_registry
from kt_qqbot.api import QQApi
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.hub import CLOSED_WINDOW, QQHub
from kt_qqbot.paths import root_dir
from kt_qqbot.plugins.router import creature_id_for
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.store import ChatStore
from kt_qqbot.trigger import QQFeedTrigger, feeds_of

from tests.conftest import APP_ID, SECRET, _NoGateway, c2c_event, group_event

pytestmark = pytest.mark.timeout(90)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = PACKAGE_ROOT / "creatures" / "qq-group-chat"


def _send(message: str, media: str = "") -> str:
    extra = f"@@media={media}\n" if media else ""
    return f"[/qq_send]\n@@message={message}\n{extra}[qq_send/]"


def _script(image: str, document: str) -> list[ScriptEntry]:
    both = _send("hi there", image) + "\n" + _send("", document)
    return [
        ScriptEntry(response=both, match="hello bot"),
        ScriptEntry(response="(private thought only)", match="group chatter"),
        ScriptEntry(response=_send("welcome back"), match="are you there"),
        ScriptEntry(response="(done)"),
    ]


async def _until(predicate, timeout=30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _is_persona_pin(message: dict) -> bool:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return False
    texts = [p.get("text", "") for p in content if p.get("type") == "text"]
    images = [p for p in content if p.get("type") == "image_url"]
    return bool(texts and texts[0].startswith("[reference image]") and images)


def _write_configs(tmp_path: Path) -> Path:
    template = tmp_path / "template"
    template.mkdir()
    (template / "config.yaml").write_text(
        f"base_config: {TEMPLATE}\n"
        "name: qq-test\n"
        "controller:\n  tool_format: bracket\n"
        "triggers:\n"
        "  - name: qq_feed\n"
        "    type: package\n"
        "    module: kt_qqbot.trigger\n"
        "    class: QQFeedTrigger\n"
        "    conversations: all\n"
        "    wake: all\n"
        "    batch_delay: 0\n"
        "compact:\n"
        "  keep_recent_turns: 1\n"
        "tools:\n"
        "  - name: qq_send\n"
        "    type: package\n"
        "    module: kt_qqbot.tools\n"
        "    class: QQSendTool\n"
        f"    media_roots: [{tmp_path}]\n",
        encoding="utf-8",
    )
    router = tmp_path / "router"
    router.mkdir()
    (router / "config.yaml").write_text(
        "name: router\n"
        "system_prompt: 'router'\n"
        "controller:\n  tool_format: bracket\n"
        "input:\n  type: none\n"
        "output:\n  type: stdout\n"
        "plugins:\n"
        "  - name: qq_router\n"
        "    type: package\n"
        "    module: kt_qqbot.plugins.router\n"
        "    class: QQRouterPlugin\n"
        "    options:\n"
        f"      template: {template}\n",
        encoding="utf-8",
    )
    return router


async def test_router_journey(tmp_path, monkeypatch, fake_api):
    CredentialStore().save(Credentials(APP_ID, SECRET))
    (root_dir() / "character.md").write_text("You are Kohaku, a cat.", encoding="utf-8")
    Image.new("RGB", (1024, 512), (240, 200, 120)).save(root_dir() / "character.png")
    store_path = tmp_path / "chat.kvault"

    def factory(creds):
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler))
        api = QQApi(
            creds,
            client,
            api_base="https://api.test",
            token_url="https://api.test/app/getAppAccessToken",
        )
        return QQHub(
            creds, api=api, store=ChatStore(store_path), gateway_factory=_NoGateway
        )

    monkeypatch.setattr(hub_registry, "HUB_FACTORY", factory)
    stickers = root_dir() / "stickers"
    stickers.mkdir(parents=True)
    (stickers / "happy.png").write_bytes(b"\x89PNG sticker")
    script = _script("m1.1", "sticker:happy.png")
    llms: dict[str, list[ScriptedLLM]] = {}

    def create(config, *args, **kwargs):
        instance = ScriptedLLM(script)
        llms.setdefault(getattr(config, "name", ""), []).append(instance)
        return instance

    monkeypatch.setattr(bootstrap_llm_mod, "create_llm_provider", create)
    monkeypatch.setattr(agent_init_mod, "create_llm_provider", create)
    router_path = _write_configs(tmp_path)
    c2c_id = creature_id_for(APP_ID, "c2c:U1")
    group_id = creature_id_for(APP_ID, "group:G1")

    engine = Terrarium(session_dir=str(tmp_path / "sessions"))
    await engine.add_creature(str(router_path))
    hub = hub_registry.running_hubs()[APP_ID]

    photo = {"content_type": "image/png", "url": "https://img.test/cat.png"}
    await hub.on_dispatch(
        EVENT_C2C, c2c_event("C1", "U1", "hello bot", attachments=[photo])
    )
    await _until(lambda: len(fake_api.sent()) >= 3)
    assert c2c_id in engine
    first = llms["qq-u_u1"][0].call_log[0]
    system = first[0]["content"]
    assert _is_persona_pin(first[1]), "the persona pin follows the system prompt"
    assert "QQ chat member" in system and "## Character" in system
    assert "You are Kohaku, a cat." in system
    delivered = first[-1]["content"]
    assert '<qq_update chat="u_u1" type="private">' in delivered
    assert "hello bot" in delivered
    body = fake_api.sent()[0]
    assert body == {"msg_type": 0, "content": "hi there", "msg_id": "C1", "msg_seq": 1}
    assert [p for m, p, _ in fake_api.requests if p.endswith("/messages")] == [
        "/v2/users/U1/messages"
    ] * 3
    image_msg, file_msg = fake_api.sent()[1:3]
    assert [image_msg["msg_type"], file_msg["msg_type"]] == [7, 7]
    assert [image_msg["msg_seq"], file_msg["msg_seq"]] == [2, 3]
    assert image_msg["msg_id"] == file_msg["msg_id"] == "C1"
    uploads = fake_api.sent("/files")
    assert uploads[0]["file_type"] == 1 and uploads[0]["url"] == photo["url"]
    assert uploads[1]["file_type"] == 1 and "file_data" in uploads[1]
    assert "[image: happy.png]" in hub.store.get("c2c:U1", 4)["content"]
    assert "[image m1.1]" in delivered
    mine = hub.store.get("c2c:U1", 2)
    assert mine["self"] and mine["by"] == c2c_id and mine["content"] == "hi there"
    assert hub._refs == 2, "router + one chat creature share the hub"

    await hub.on_dispatch(EVENT_GROUP, group_event("G-1", "G1", "P1", "group chatter"))
    await _until(lambda: any(llm.call_count for llm in llms.get("qq-g_g1", [])))
    assert group_id in engine
    group_chat = engine.get_creature(group_id)
    group_llm = llms["qq-g_g1"][-1]
    turns = len(group_llm.call_log)
    await hub.on_dispatch(EVENT_GROUP, group_event("G-1b", "G1", "P2", "more chatter"))
    await _until(lambda: len(group_llm.call_log) > turns)
    await asyncio.sleep(0.2)
    before = len(group_chat.agent.controller.conversation.get_messages())
    assert group_chat.agent.compact_manager.trigger_compact(), "compaction runs"
    await group_chat.agent.compact_manager.wait_for_current()
    assert len(group_chat.agent.controller.conversation.get_messages()) < before
    calls_before = len(group_llm.call_log)
    await hub.on_dispatch(EVENT_GROUP, group_event("G-2", "G1", "P1", "after compact"))
    await _until(lambda: len(group_llm.call_log) > calls_before)
    after = group_llm.call_log[-1]
    assert _is_persona_pin(after[1]), "the pin survives compaction"
    assert sum(_is_persona_pin(m) for m in after) == 1
    await asyncio.sleep(0.3)
    assert not [p for m, p, _ in fake_api.requests if p.startswith("/v2/groups")]

    hub.remember_identity({"id": "BOTJ", "username": "kt-journey"})
    first_meta = hub.store.conv_meta("c2c:U1")
    await engine.shutdown()
    assert hub_registry.running_hubs() == {}

    # Restart on the SAME session dir: chats come back with their history.
    engine2 = Terrarium(session_dir=str(tmp_path / "sessions"))
    await engine2.add_creature(str(router_path))
    await _until(lambda: c2c_id in engine2 and group_id in engine2)
    hub2 = hub_registry.running_hubs()[APP_ID]
    assert hub2.bot_id == "BOTJ" and hub2.bot_identity["username"] == "kt-journey"
    meta = hub2.store.conv_meta("c2c:U1")
    assert meta["first_seen"] == first_meta["first_seen"] and meta["kind"] == "c2c"
    assert hub2.store.handle("c2c:U1") == "u_u1"
    assert hub2._refs == 3, "router + two chat creatures, one ref each"
    restored_chat = engine2.get_creature(c2c_id)
    assert len(feeds_of(restored_chat.agent)) == 1
    history = [
        str(m.content)
        for m in restored_chat.agent.controller.conversation.get_messages()
    ]
    assert any("hello bot" in h for h in history), "the conversation came back"
    restored = hub2.store.slots()
    assert restored["C1"]["used"] == 3 and restored["C1"]["last_seq"] == 3
    await hub2.on_dispatch(EVENT_C2C, c2c_event("C2", "U1", "are you there"))
    await _until(lambda: len(fake_api.sent()) == 4)
    resumed = fake_api.sent()[3]
    assert resumed["content"] == "welcome back" and resumed["msg_id"] == "C2"
    for _ in range(3):
        assert (await hub2.send("c2c:U1", "more")).passive
    spill = await hub2.send("c2c:U1", "spill")
    assert spill.passive
    tail = fake_api.sent()[-1]
    assert tail["msg_id"] == "C1" and tail["msg_seq"] == 4, "restart kept C1's seq"
    assert hub2.store.slots()["C2"]["used"] == 4
    fresh_llm = llms["qq-u_u1"][-1]
    first_call = fresh_llm.call_log[0]
    assert _is_persona_pin(first_call[1]), "the pin is restored after the restart"
    assert sum(_is_persona_pin(m) for m in first_call) == 1, "restored once"
    users = [
        str(m.get("content"))
        for m in first_call
        if m["role"] == "user" and not _is_persona_pin(m)
    ]
    assert any("hello bot" in u for u in users[:-1]), "history is in the prompt"
    assert "are you there" in users[-1]
    assert "hello bot" not in users[-1], "the cursor survived the restart"

    # Full-message mode: every group message is GROUP_MESSAGE_CREATE.
    mention_feed = QQFeedTrigger(
        wake="mention", batch_delay=0, conversations=["group:GM"], subscriber="jm"
    )
    await mention_feed.attach(hub2)
    await mention_feed.start()
    await hub2.on_dispatch(EVENT_GROUP, group_event("GM-1", "GM", "P1", "chatter"))
    assert await mention_feed.wait_for_trigger() is None, "plain chat does not wake"
    await hub2.on_dispatch(
        EVENT_GROUP,
        group_event("GM-2", "GM", "P2", "hey bot", mentions=[{"is_you": True}]),
    )
    woke = await mention_feed.wait_for_trigger()
    assert woke is not None and woke.context["mention"] is True
    assert "hey bot" in woke.content
    await mention_feed.stop()
    await hub2.on_dispatch(EVENT_GROUP, group_event("GP-1", "GP", "P3", "plain"))
    before = len(fake_api.sent())
    for i in range(5):
        assert (await hub2.send("group:GP", f"r{i}")).sent
    bodies = fake_api.sent()[before:]
    assert [(b["msg_id"], b["msg_seq"]) for b in bodies] == [
        ("GP-1", n) for n in range(1, 6)
    ]
    sixth = await hub2.send("group:GP", "one too many")
    assert not sixth.sent and sixth.reason == CLOSED_WINDOW
    assert len(fake_api.sent()) == before + 5, "no active message is attempted"
    await engine2.shutdown()
    assert hub_registry.running_hubs() == {}

    # Dashboard: the package and plugin owners through the core owner RPC.
    _dashboard_phase(tmp_path, monkeypatch, template_dir=tmp_path / "template")


def _dashboard_phase(tmp_path: Path, monkeypatch, template_dir: Path) -> None:
    async def fake_check(creds, client=None):
        return None

    monkeypatch.setattr(qq_api, "check_credentials", fake_check)
    install_package(str(PACKAGE_ROOT), editable=True, deps="never")
    session_dir = tmp_path / "sessions3"
    session_dir.mkdir()
    monkeypatch.setenv("KT_SESSION_DIR", str(session_dir))
    set_service(LocalTerrariumService(Terrarium(session_dir=str(session_dir))))
    package_ref = {"owner": "package:kt-qqbot"}
    try:
        with TestClient(create_app()) as client:
            listing = client.get("/api/extensions/ui").json()
            entries = (
                listing if isinstance(listing, list) else listing.get("packages", [])
            )
            ours = next(p for p in entries if p["package"] == "kt-qqbot")
            assert ours["error"] == "" and ours["enabled"] is True
            kinds = sorted((c["kind"], c["owner"]) for c in ours["contributes"])
            assert kinds == [
                ("panel", "plugin:qq_status"),
                ("settings", "package:kt-qqbot"),
                ("tool-renderer", "tool:qq_send"),
            ]

            resp = client.post("/api/extensions/owner/state", json=package_ref)
            assert resp.status_code == 200, resp.text
            bots = resp.json()["state"]["bots"]
            assert [b["app_id"] for b in bots] == [APP_ID]
            assert bots[0]["gateway"] == "not running"
            assert SECRET not in resp.text

            resp = client.post(
                "/api/sessions/active/agents", json={"config_path": str(template_dir)}
            )
            assert resp.status_code == 200, resp.text
            panel_ref = {
                "owner": "plugin:qq_status",
                "session_id": resp.json()["session_id"],
                "creature_id": "qq-test",
            }
            hub = hub_registry.running_hubs()[APP_ID]
            client.portal.call(
                hub.on_dispatch, EVENT_C2C, c2c_event("C9", "U9", "panel ping")
            )
            state = client.post("/api/extensions/owner/state", json=panel_ref).json()
            feed = state["state"]["feeds"][0]
            u9 = next(c for c in feed["chats"] if c["handle"] == "u_u9")
            assert u9["free_slots"] == 4 and u9["last"][-1]["text"] == "panel ping"
            pkg_state = client.post("/api/extensions/owner/state", json=package_ref)
            assert pkg_state.json()["state"]["bots"][0]["gateway"] == "test"

            with client.websocket_connect("/ws/extensions/owner") as ws:
                ws.send_json({"op": "subscribe", "id": "p", **panel_ref})
                assert ws.receive_json()["type"] == "subscribed"
                ws.send_json({"op": "subscribe", "id": "k", **package_ref})
                assert ws.receive_json()["type"] == "subscribed"
                client.portal.call(
                    hub.on_dispatch, EVENT_C2C, c2c_event("C10", "U9", "second ping")
                )
                push = ws.receive_json()
                while push.get("id") != "p":
                    push = ws.receive_json()
                chats = push["data"]["feeds"][0]["chats"]
                assert (
                    next(c for c in chats if c["handle"] == "u_u9")["last"][-1]["text"]
                    == "second ping"
                )

                tested = client.post(
                    "/api/extensions/owner/call",
                    json={**package_ref, "action": "test", "args": {}},
                ).json()["result"]
                assert tested["ok"] is True
                unbound = client.post(
                    "/api/extensions/owner/call",
                    json={
                        **package_ref,
                        "action": "unbind",
                        "args": {"app_id": APP_ID},
                    },
                ).json()["result"]
                assert unbound == {"ok": True, "app_id": APP_ID}
                push = ws.receive_json()
                while not (push.get("id") == "k" and push.get("event") == "state"):
                    push = ws.receive_json()
                assert push["data"]["bots"] == []
                assert hub.status == "unbound"
                bad = client.post(
                    "/api/extensions/owner/call",
                    json={**package_ref, "action": "explode", "args": {}},
                )
                assert bad.status_code == 400

        # Optional multi-user: an anonymous caller may read, not change.
        monkeypatch.setenv("KT_AUTH_MULTI_USER", "optional")
        set_enabled("kt-qqbot", True)
        set_service(LocalTerrariumService(Terrarium(session_dir=str(session_dir))))
        with TestClient(create_app()) as client:
            resp = client.post("/api/extensions/owner/state", json=package_ref)
            assert resp.status_code == 200, resp.text
            assert resp.json()["state"]["bind"]["url"] == ""
            for action in ("bind_start", "unbind", "save_manual"):
                resp = client.post(
                    "/api/extensions/owner/call",
                    json={**package_ref, "action": action, "args": {}},
                )
                assert resp.status_code == 403, (action, resp.text)
                assert resp.json()["detail"]["error"] == "owner_forbidden"
    finally:
        set_service(None)
