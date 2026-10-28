"""Hub lifecycle and registry, media guards, render markers, CLI manual login."""

import asyncio

import httpx
import pytest

from kt_qqbot import cli, media
from kt_qqbot import hub as hub_registry
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import AuthFailed, GatewayFatal
from kt_qqbot.media import BlockedAddress, ImageOptions, check_public_url
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.render import render_line

from tests.conftest import APP_ID, SECRET, c2c_event, group_event

pytestmark = pytest.mark.timeout(20)


class FatalGateway:
    def __init__(self, api, on_dispatch):
        self.state = "connecting"
        self.stopped = False

    async def run(self):
        self.state = "fatal"
        raise GatewayFatal(4914, "offline")

    def stop(self):
        self.stopped = True


class TestHubLifecycle:
    async def test_fatal_gateway_is_logged_and_close_is_clean(self, make_hub):
        hub = make_hub()
        hub.gateway = FatalGateway(hub.api, hub.on_dispatch)
        hub.start()
        await asyncio.sleep(0.01)
        assert hub.status == "fatal"
        await hub.close()
        assert hub.gateway.stopped

    async def test_typing_only_for_private_chats(self, make_hub, fake_api):
        hub = make_hub()
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "hi"))
        await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G1", "P", "hi"))
        await hub.typing("c2c:U1")
        await hub.typing("group:G1")
        bodies = fake_api.sent()
        assert len(bodies) == 1 and bodies[0]["msg_type"] == 6
        assert bodies[0]["input_notify"]["input_second"] == 60
        await hub.close()

    async def test_registry_refcounts_one_hub_per_bot(self, make_hub, monkeypatch):
        CredentialStore().save(Credentials(APP_ID, SECRET))
        built = []

        def factory(creds):
            built.append(make_hub())
            return built[-1]

        monkeypatch.setattr(hub_registry, "HUB_FACTORY", factory)
        first = await hub_registry.acquire()
        second = await hub_registry.acquire(APP_ID)
        assert first is second and len(built) == 1
        await hub_registry.release(first)
        assert hub_registry.running_hubs() == {APP_ID: first}
        await hub_registry.release(second)
        assert hub_registry.running_hubs() == {}

    async def test_new_conversation_callback_errors_do_not_break_ingest(self, make_hub):
        hub = make_hub()

        async def broken(cid):
            raise RuntimeError("boom")

        hub.on_new_conversation(broken)
        assert await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U", "x")) is None
        assert hub.store.head("c2c:U") == 1
        hub.remove_new_conversation(broken)
        await hub.close()


class TestMedia:
    async def test_address_guard(self):
        with pytest.raises(BlockedAddress, match="https"):
            await check_public_url("http://example.com/a.png")
        with pytest.raises(BlockedAddress, match="non-public"):
            await check_public_url("https://127.0.0.1/a.png")
        with pytest.raises(BlockedAddress, match="non-public"):
            await check_public_url("https://[::1]/a.png")
        with pytest.raises(BlockedAddress, match="cannot resolve"):
            await check_public_url("https://no-such-host.invalid/a.png")

    async def test_size_cap_and_non_images(self, monkeypatch, tmp_path):
        async def allow(url):
            return None

        monkeypatch.setattr(media, "check_public_url", allow)
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"x" * 100)
            )
        )
        messages = [
            {
                "seq": 1,
                "attachments": [
                    {"content_type": "video/mp4", "url": "https://a/v.mp4"},
                    {"content_type": "image/png", "url": "https://a/big.png"},
                ],
            }
        ]
        parts = await media.images_for(client, messages, ImageOptions(max_bytes=10))
        assert parts == []
        parts = await media.images_for(client, messages, ImageOptions(max_bytes=1000))
        assert len(parts) == 1
        assert (
            await media.images_for(client, messages, ImageOptions(enabled=False)) == []
        )
        await client.aclose()


def test_render_media_markers():
    msg = {
        "seq": 1,
        "alias": "#a",
        "content": "files",
        "attachments": [
            {"content_type": "voice"},
            {"content_type": "video/mp4"},
            {"content_type": "application/pdf", "filename": "a.pdf"},
        ],
        "reply_to": "OLD",
    }
    line = render_line(msg)
    assert line.endswith("files [voice] [video] [file:a.pdf]")
    assert "↩(older message)" in line


class TestCliManual:
    def test_manual_login_checks_then_saves(self, monkeypatch, capsys):
        answers = iter(["123"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "pw")
        checked = []

        async def ok(creds):
            checked.append(creds)

        monkeypatch.setattr(cli, "_check", ok)
        assert cli.main(["login", "--manual"]) == 0
        assert checked == [Credentials("123", "pw")]
        assert CredentialStore().load("123").app_secret == "pw"

    def test_manual_login_rejects_bad_secret(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt="": "123")
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "bad")

        async def reject(creds):
            raise AuthFailed("123", "invalid secret")

        monkeypatch.setattr(cli, "_check", reject)
        assert cli.main(["login", "--manual"]) == 1
        assert "Not saved" in capsys.readouterr().err
        assert CredentialStore().list_app_ids() == []

    def test_manual_login_needs_both_fields(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "")
        assert cli.main(["login", "--manual"]) == 1
