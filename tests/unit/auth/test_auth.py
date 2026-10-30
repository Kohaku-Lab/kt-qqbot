"""Crypto, credential store, and the QR bind flow."""

import base64
import json
import os
import stat

import httpx
import pytest

from kt_qqbot.auth import bind
from kt_qqbot.auth.bind import bind_flow, connect_url
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.auth.crypto import decrypt_secret, encrypt_secret, new_key
from kt_qqbot.errors import BindError, CredentialsMissing


class TestCrypto:
    def test_round_trip(self):
        key = new_key()
        assert len(key) == 32
        blob = encrypt_secret("app-secret-123", key)
        raw = base64.b64decode(blob)
        assert len(raw) == 12 + len("app-secret-123") + 16
        assert decrypt_secret(blob, key) == "app-secret-123"

    def test_wrong_key_and_tampering_fail(self):
        key = new_key()
        blob = encrypt_secret("x" * 20, key)
        with pytest.raises(BindError, match="authentication"):
            decrypt_secret(blob, new_key())
        raw = bytearray(base64.b64decode(blob))
        raw[15] ^= 1
        with pytest.raises(BindError):
            decrypt_secret(base64.b64encode(bytes(raw)).decode(), key)

    def test_malformed_blobs(self):
        with pytest.raises(BindError, match="too short"):
            decrypt_secret(base64.b64encode(b"short").decode(), new_key())
        with pytest.raises(BindError, match="base64"):
            decrypt_secret("!!!not base64", new_key())


class TestCredentialStore:
    def test_save_load_permissions_and_default(self, tmp_path):
        store = CredentialStore(tmp_path / "c.json")
        store.save(Credentials("1", "a"))
        store.save(Credentials("2", "hidden-b"), make_default=False)
        if os.name == "posix":
            assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert store.load() == Credentials("1", "a")
        assert store.load("2") == Credentials("2", "hidden-b")
        assert store.list_app_ids() == ["1", "2"]
        assert store.remove() == "1"
        assert store.default_app_id() == "2"
        assert store.remove("nope") is None
        assert "hidden-b" not in repr(store.load())

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        store = CredentialStore(tmp_path / "c.json")
        store.save(Credentials("1", "file"))
        monkeypatch.setenv("QQBOT_APPID", "9")
        monkeypatch.setenv("QQBOT_SECRET", "env")
        assert store.load() == Credentials("9", "env")
        assert store.load("1") == Credentials("1", "file")

    def test_missing_names_the_login_command(self, tmp_path):
        with pytest.raises(CredentialsMissing, match="kt-qqbot login"):
            CredentialStore(tmp_path / "none.json").load()


class BindServer:
    """Fake q.qq.com bind endpoints; encrypts the secret with the client's key."""

    def __init__(self, polls):
        self.polls = list(polls)
        self.keys: list[bytes] = []
        self.created = 0
        self.poll_tasks: list[str] = []
        self.create_retcode = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/lite/create_bind_task":
            self.keys.append(base64.b64decode(body["key"]))
            self.created += 1
            return httpx.Response(
                200,
                json={
                    "retcode": self.create_retcode,
                    "data": {"task_id": f"T{self.created}"},
                },
            )
        self.poll_tasks.append(body["task_id"])
        step = self.polls.pop(0)
        if step == "neterror":
            return httpx.Response(502, text="bad gateway")
        if step == "done":
            data = {
                "status": 2,
                "bot_appid": "777",
                "bot_encrypt_secret": encrypt_secret("SECRET777", self.keys[-1]),
                "user_openid": "U",
            }
        else:
            data = {"status": step}
        return httpx.Response(200, json={"retcode": 0, "data": data})


async def _run(server, tmp_path, **kwargs):
    store = CredentialStore(tmp_path / "c.json")
    async with httpx.AsyncClient(transport=httpx.MockTransport(server.handler)) as c:
        steps = [
            s async for s in bind_flow(client=c, store=store, poll_interval=0, **kwargs)
        ]
    return steps, store


class TestBindFlow:
    async def test_expiry_renews_qr_then_binds(self, tmp_path):
        server = BindServer([1, "neterror", 3, 0, 1, "done"])
        steps, store = await _run(server, tmp_path)
        kinds = [s.kind for s in steps]
        assert kinds == ["show_qr", "progress", "show_qr", "done"]
        assert steps[0].url == connect_url("T1")
        assert "source=kohakuterrarium" in steps[0].url and "_wv=2" in steps[0].url
        assert steps[2].url == connect_url("T2")
        assert server.poll_tasks == ["T1", "T1", "T1", "T2", "T2", "T2"]
        assert server.keys[0] != server.keys[1]
        assert steps[-1].app_id == "777"
        assert store.load("777").app_secret == "SECRET777"
        assert all("SECRET777" not in repr(s) for s in steps)

    async def test_create_failure_is_an_error_step(self, tmp_path):
        server = BindServer([])
        server.create_retcode = 5
        steps, store = await _run(server, tmp_path)
        assert [s.kind for s in steps] == ["error"]
        assert store.list_app_ids() == []

    async def test_timeout_ends_with_error(self, tmp_path):
        server = BindServer([1] * 1000)
        steps, _ = await _run(server, tmp_path, timeout=0.05)
        assert steps[0].kind == "show_qr"
        assert steps[-1].kind == "error" and "timed out" in steps[-1].message

    async def test_bad_secret_is_not_saved(self, tmp_path, monkeypatch):
        server = BindServer(["done"])
        monkeypatch.setattr(bind, "decrypt_secret", _raise_bind)
        steps, store = await _run(server, tmp_path)
        assert steps[-1].kind == "error"
        assert store.list_app_ids() == []


def _raise_bind(*_args):
    raise BindError("bind secret failed authentication")
