"""The package UI owner (settings), the qq_status panel owner, hub reloads."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from kohakuterrarium.core.trigger_manager import TriggerManager
from kohakuterrarium.api.ext import UICallContext
from kohakuterrarium.modules.ui_owner import (
    OwnerForbiddenError,
    bind_publisher,
    owner_call,
    owner_state,
)

from kt_qqbot import api as qq_api
from kt_qqbot import hub as hub_registry
from kt_qqbot import ui_owner
from kt_qqbot.auth import bind as qq_bind
from kt_qqbot.auth.bind import BindStep
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import (
    AdminRequired,
    AuthFailed,
    BadOwnerArgument,
)
from kt_qqbot.plugins import status as status_mod
from kt_qqbot.plugins.status import QQStatusPlugin
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.trigger import QQFeedTrigger
from kt_qqbot.ui_owner import QQBotUI

from tests.conftest import APP_ID, SECRET, c2c_event, group_event

pytestmark = pytest.mark.timeout(20)


def _ctx(user_id, *, admin=False, multi_user=True):
    return UICallContext(
        user_id=user_id,
        role="admin" if admin else ("user" if user_id is not None else ""),
        owner="package:kt-qqbot",
        creature_id="",
        multi_user=multi_user,
        is_admin=admin,
    )


USER = _ctx(7)
ADMIN = _ctx(1, admin=True)
ANONYMOUS = _ctx(None)
SINGLE_USER = _ctx(None, multi_user=False)


def _recorder(owner, targets=None):
    events: list[tuple[str, object]] = []

    def publish(event, data, user_id=None):
        events.append((event, data))
        if targets is not None:
            targets.append((event, user_id) if event == "bind" else user_id)

    bind_publisher(owner, publish)
    return events


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.fixture
def fake_check(monkeypatch):
    checked = []

    async def check(creds, client=None):
        checked.append(creds)
        if creds.app_secret != SECRET:
            raise AuthFailed(creds.app_id, "invalid secret")

    monkeypatch.setattr(qq_api, "check_credentials", check)
    return checked


class TestSettingsOwner:
    async def test_state_never_contains_a_secret(self, monkeypatch, fake_check):
        owner = QQBotUI()
        CredentialStore().save(Credentials(APP_ID, SECRET))
        monkeypatch.setenv("QQBOT_APPID", "9")
        monkeypatch.setenv("QQBOT_SECRET", "env-secret")
        state = await owner_state(owner)
        assert [b["app_id"] for b in state["bots"]] == [APP_ID, "9"]
        assert state["bots"][0]["gateway"] == "not running"
        assert state["bots"][1]["source"] == "env"
        result = await owner_call(
            owner, "save_manual", {"app_id": "55", "app_secret": SECRET}
        )
        assert result == {"ok": True, "app_id": "55"}
        dumped = json.dumps([await owner_state(owner), result])
        assert SECRET not in dumped and "env-secret" not in dumped
        assert CredentialStore().load("55").app_secret == SECRET

    async def test_admin_gate(self, fake_check):
        owner = QQBotUI()
        CredentialStore().save(Credentials(APP_ID, SECRET))
        for action, args in [
            ("bind_start", {}),
            ("bind_cancel", {}),
            ("save_manual", {"app_id": "1", "app_secret": SECRET}),
            ("unbind", {"app_id": APP_ID}),
        ]:
            with pytest.raises(AdminRequired):
                await owner_call(owner, action, args, USER)
        assert issubclass(AdminRequired, OwnerForbiddenError)
        assert CredentialStore().list_app_ids() == [APP_ID]
        assert (await owner_call(owner, "test", {}, USER))["ok"] is True
        assert "bots" in await owner_call(owner, "refresh", {}, USER)
        assert (await owner_call(owner, "bind_cancel", {}, None))["ok"] is False
        with pytest.raises(BadOwnerArgument, match="unknown action"):
            await owner_call(owner, "explode", {}, ADMIN)

    async def test_manual_entry_rejected_and_validated(self, fake_check):
        owner = QQBotUI()
        bad = await owner_call(
            owner, "save_manual", {"app_id": "1", "app_secret": "no"}
        )
        assert bad["ok"] is False and "invalid secret" in bad["message"]
        assert CredentialStore().list_app_ids() == []
        with pytest.raises(BadOwnerArgument, match="both required"):
            await owner_call(owner, "save_manual", {"app_id": "1"})
        with pytest.raises(BadOwnerArgument, match="too long"):
            await owner_call(
                owner, "save_manual", {"app_id": "1" * 65, "app_secret": "x"}
            )
        missing = await owner_call(owner, "test", {"app_id": "nope"})
        assert missing["ok"] is False and "kt-qqbot login" in missing["message"]

    async def test_bind_steps_are_pushed_in_order_and_reload_the_hub(
        self, monkeypatch, make_hub
    ):
        CredentialStore().save(Credentials(APP_ID, "old-secret"))
        hub = make_hub()
        hub_registry._HUBS[APP_ID] = hub

        async def fake_flow(*, store=None, **kwargs):
            yield BindStep("show_qr", url="https://q.qq.com/a?task_id=T1")
            yield BindStep("progress", message="QR code expired; showing a new one.")
            yield BindStep("show_qr", url="https://q.qq.com/a?task_id=T2")
            store.save(Credentials(APP_ID, SECRET))
            yield BindStep("done", app_id=APP_ID, message=f"Bound bot {APP_ID}.")

        monkeypatch.setattr(qq_bind, "bind_flow", fake_flow)
        owner = QQBotUI()
        events = _recorder(owner)
        assert await owner_call(owner, "bind_start", {}) == {"ok": True}
        await _until(lambda: owner._task is None)
        binds = [d for e, d in events if e == "bind"]
        assert [b["kind"] for b in binds] == ["show_qr", "progress", "show_qr", "done"]
        assert binds[2]["url"].endswith("T2")
        phases = [d["bind"]["phase"] for e, d in events if e == "state"]
        assert phases == ["starting", "waiting", "waiting", "waiting", "done"]
        assert hub.creds.app_secret == SECRET, "the running hub got the new secret"
        assert all(SECRET not in json.dumps(d) for _, d in events)
        await hub.close()

    async def test_cancel_stops_the_flow(self, monkeypatch):
        closed = asyncio.Event()

        async def endless_flow(**kwargs):
            try:
                yield BindStep("show_qr", url="https://q.qq.com/a?task_id=T1")
                await asyncio.Event().wait()
            finally:
                closed.set()

        monkeypatch.setattr(qq_bind, "bind_flow", endless_flow)
        owner = QQBotUI()
        events = _recorder(owner)
        await owner_call(owner, "bind_start", {})
        await _until(lambda: owner.ui_state()["bind"]["phase"] == "waiting")
        task = owner._task
        assert (await owner_call(owner, "bind_cancel", {})) == {"ok": True}
        assert task.cancelled() and closed.is_set()
        assert owner.ui_state()["bind"]["phase"] == "cancelled"
        assert events[-2] == (
            "bind",
            {"kind": "cancelled", "message": "Binding cancelled."},
        )

    async def test_restart_replaces_a_running_flow(self, monkeypatch):
        starts = []

        async def flow(**kwargs):
            starts.append(1)
            yield BindStep("show_qr", url=f"https://q.qq.com/a?task_id=T{len(starts)}")
            await asyncio.Event().wait()

        monkeypatch.setattr(qq_bind, "bind_flow", flow)
        owner = QQBotUI()
        events = _recorder(owner)
        await owner_call(owner, "bind_start", {})
        await _until(lambda: owner.ui_state()["bind"]["url"].endswith("T1"))
        first = owner._task
        await owner_call(owner, "bind_start", {})
        await _until(lambda: owner.ui_state()["bind"]["url"].endswith("T2"))
        assert first.cancelled()
        kinds = [d["kind"] for e, d in events if e == "bind"]
        assert kinds == ["show_qr", "cancelled", "show_qr"], "the old flow is reported"
        await owner_call(owner, "bind_cancel", {})

    async def test_aclose_cancels_a_running_bind(self, monkeypatch):
        async def flow(**kwargs):
            yield BindStep("show_qr", url="https://q.qq.com/a?task_id=T1")
            await asyncio.Event().wait()

        monkeypatch.setattr(qq_bind, "bind_flow", flow)
        owner = QQBotUI()
        await owner_call(owner, "bind_start", {})
        task = owner._task
        await owner.aclose()
        assert task.cancelled() and owner._task is None

    async def test_multi_user_pushes_go_to_the_binding_admin_only(
        self, monkeypatch, fake_check
    ):
        async def flow(**kwargs):
            yield BindStep("show_qr", url="https://q.qq.com/a?task_id=T1")
            await asyncio.Event().wait()

        monkeypatch.setattr(qq_bind, "bind_flow", flow)
        owner = QQBotUI()
        targets: list = []
        events = _recorder(owner, targets)
        await owner_call(owner, "bind_start", {}, ADMIN)
        await _until(lambda: owner.ui_state(ADMIN)["bind"]["url"])
        assert ("bind", ADMIN.user_id) in targets
        assert {t for t in targets if not isinstance(t, tuple)} == {ADMIN.user_id}
        assert owner.ui_state(_ctx(2, admin=True))["bind"]["url"], "admins see it"
        hidden = owner.ui_state(USER)["bind"]
        assert hidden["url"] == "" and hidden["phase"] == "waiting"
        assert owner.ui_state(ANONYMOUS)["bind"]["url"] == ""
        assert owner.ui_state(SINGLE_USER)["bind"]["url"]
        await owner_call(owner, "bind_cancel", {}, ADMIN)
        binds = [t for t in targets if isinstance(t, tuple)]
        assert binds == [("bind", ADMIN.user_id)] * 2, "bind steps reach the starter"
        assert events[-1][0] == "state"
        before = len(targets)
        CredentialStore().save(Credentials(APP_ID, SECRET))
        assert (await owner_call(owner, "unbind", {"app_id": APP_ID}, ADMIN))["ok"]
        assert sorted(targets[before:]) == [1, 2, 7], "state goes to every viewer"

    def test_require_admin_helper(self):
        ui_owner.require_admin(None)
        ui_owner.require_admin(SINGLE_USER)
        ui_owner.require_admin(ADMIN)
        for context in (USER, ANONYMOUS):
            with pytest.raises(OwnerForbiddenError):
                ui_owner.require_admin(context)

    async def test_anonymous_under_optional_multi_user_is_forbidden(self, fake_check):
        owner = QQBotUI()
        CredentialStore().save(Credentials(APP_ID, SECRET))
        for action, args in [
            ("bind_start", {}),
            ("bind_cancel", {}),
            ("save_manual", {"app_id": "1", "app_secret": SECRET}),
            ("unbind", {"app_id": APP_ID}),
        ]:
            with pytest.raises(OwnerForbiddenError):
                await owner_call(owner, action, args, ANONYMOUS)
        assert CredentialStore().list_app_ids() == [APP_ID]
        assert owner._task is None

    async def test_state_pushes_go_to_each_signed_in_viewer(self, fake_check):
        owner = QQBotUI()
        targets: list = []
        events = _recorder(owner, targets)
        await owner_state(owner, USER)
        await owner_state(owner, ANONYMOUS)
        await owner_call(
            owner, "save_manual", {"app_id": "5", "app_secret": SECRET}, ADMIN
        )
        assert sorted(targets) == sorted([USER.user_id, ADMIN.user_id])
        assert all(e == "state" for e, _ in events)
        single = QQBotUI()
        single_targets: list = []
        _recorder(single, single_targets)
        await owner_call(
            single, "save_manual", {"app_id": "6", "app_secret": SECRET}, SINGLE_USER
        )
        assert single_targets == [None], "single-user hosts push untargeted"


class TestHubCredentialReload:
    async def test_unbind_and_rebind_running_hub(self, make_hub, fake_api):
        CredentialStore().save(Credentials(APP_ID, SECRET))
        hub = make_hub()
        hub_registry._HUBS[APP_ID] = hub
        changes = []
        hub.add_listener(lambda kind, cid: changes.append(kind))
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "hi"))
        CredentialStore().remove(APP_ID)
        await hub_registry.apply_credentials(APP_ID)
        assert hub.status == "unbound"
        refused = await hub.send("c2c:U1", "x")
        assert not refused.sent and "unbound" in refused.reason
        CredentialStore().save(Credentials(APP_ID, SECRET))
        await hub_registry.apply_credentials(APP_ID)
        assert hub.status != "unbound"
        assert (await hub.send("c2c:U1", "back")).sent
        assert (
            changes[0] == "message" and "gateway" in changes and changes[-1] == "sent"
        )
        await hub_registry.apply_credentials("not-running")
        hub_registry._HUBS.clear()
        await hub.close()


class TestStatusPanelOwner:
    async def test_state_and_live_pushes(self, make_hub, tmp_path):
        hub = make_hub()
        feed = QQFeedTrigger(batch_delay=0, wake="all")
        await feed.attach(hub)
        await feed.start()
        manager = TriggerManager(process_event=None)
        manager._triggers["qq_feed"] = feed
        plugin = QQStatusPlugin(last_messages=2)
        await plugin.on_load(
            SimpleNamespace(host_agent=SimpleNamespace(trigger_manager=manager))
        )
        events = _recorder(plugin)
        await plugin.on_agent_start()

        await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G1", "P1", "one"))
        await hub.on_dispatch(EVENT_GROUP, group_event("G2", "G1", "P2", "two"))
        await hub.on_dispatch(EVENT_GROUP, group_event("G3", "G1", "P1", "three"))
        await _until(lambda: events and events[-1][1]["feeds"][0]["chats"])
        await asyncio.sleep(0.05)
        chat = events[-1][1]["feeds"][0]["chats"][0]
        assert chat["handle"] == "g_g1" and chat["type"] == "group"
        assert chat["unread"] == 3 and chat["seen"] is False
        assert chat["free_slots"] == 12
        assert [m["text"] for m in chat["last"]] == ["two", "three"]
        assert events[-1][1]["feeds"][0]["gateway"] == "test"

        await feed.wait_for_trigger()
        await hub.send("group:G1", "mine", by=feed.subscriber)
        await _until(lambda: events[-1][1]["feeds"][0]["chats"][0]["free_slots"] == 11)
        chat = events[-1][1]["feeds"][0]["chats"][0]
        assert chat["seen"] is True and chat["unread"] == 0
        assert chat["last"][-1] == {**chat["last"][-1], "sender": "you", "self": True}

        state = await owner_call(plugin, "refresh", {})
        assert state == plugin.ui_state() and events[-1] == ("state", state)
        with pytest.raises(BadOwnerArgument):
            await owner_call(plugin, "nope", {})
        await plugin.on_agent_stop()
        assert plugin._on_hub_change not in hub._listeners
        await feed.stop()
        await hub.close()

    async def test_no_feed_means_empty_state(self):
        plugin = QQStatusPlugin()
        assert plugin.ui_state() == {"feeds": []}
        await plugin.on_load(SimpleNamespace(host_agent=None))
        assert plugin.ui_state() == {"feeds": []}


async def test_status_plugin_is_idle_without_hub_changes(make_hub, monkeypatch):
    monkeypatch.setattr(status_mod, "RESCAN_S", 0.02)
    monkeypatch.setattr(status_mod, "DEBOUNCE_S", 0.01)
    hub = make_hub()
    feed = QQFeedTrigger(batch_delay=0, wake="all")
    await feed.attach(hub)
    await feed.start()
    manager = TriggerManager(process_event=None)
    manager._triggers["qq_feed"] = feed
    plugin = QQStatusPlugin()
    await plugin.on_load(
        SimpleNamespace(host_agent=SimpleNamespace(trigger_manager=manager))
    )
    computed = []
    real = plugin._feed_state
    monkeypatch.setattr(plugin, "_feed_state", lambda f: computed.append(1) or real(f))
    await plugin.on_agent_start()
    await asyncio.sleep(0.1)
    baseline = len(computed)
    await asyncio.sleep(0.2)
    assert len(computed) == baseline, "no hub change, no recompute"
    await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "x"))
    await _until(lambda: len(computed) > baseline)
    await plugin.on_agent_stop()
    await feed.stop()
    await hub.close()
