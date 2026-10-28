"""Shared fixtures: an isolated KT config root and a fake QQ OpenAPI.

The fake OpenAPI is an ``httpx.MockTransport``; everything above the HTTP
boundary (token cache, ledger, store, trigger, tools) runs for real.
"""

import json
import os
from datetime import datetime, timezone
from itertools import count

import httpx
import pytest

from kt_qqbot import hub as hub_registry
from kt_qqbot.api import QQApi
from kt_qqbot.auth.credentials import Credentials
from kt_qqbot.hub import QQHub
from kt_qqbot.store import ChatStore

from tests import safety

APP_ID = "102000001"
SECRET = "s3cret-value"


@pytest.fixture(autouse=True)
def safety_guard(request, tmp_path, monkeypatch):
    """No real provider, credential file or network in any test (see tests/safety.py)."""
    live = request.node.get_closest_marker("live") and os.environ.get("KT_QQBOT_LIVE")
    if live:
        yield
        return
    safety.install(monkeypatch, tmp_path)
    yield
    safety.uninstall()


@pytest.fixture(autouse=True)
def isolated_config(safety_guard, tmp_path, monkeypatch):
    """KT config dir under tmp, no QQ env credentials, empty hub registry."""
    monkeypatch.setenv("KT_CONFIG_DIR", str(tmp_path / "ktconfig"))
    monkeypatch.delenv("QQBOT_APPID", raising=False)
    monkeypatch.delenv("QQBOT_SECRET", raising=False)
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(key, "2")
    hub_registry._HUBS.clear()
    yield
    hub_registry._HUBS.clear()


class FakeOpenAPI:
    """Record QQ OpenAPI requests and answer them like the platform."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.token_calls = 0
        self.fail_send: str | None = None
        self.fail_upload: str | None = None
        self.fail_codes: list[int] = []
        self._ids = count(1)

    def sent(self, suffix: str = "/messages") -> list[dict]:
        """Return the JSON bodies of POSTs whose path ends with ``suffix``."""
        return [b for m, p, b in self.requests if m == "POST" and p.endswith(suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.requests.append((request.method, path, body))
        if path == "/app/getAppAccessToken":
            self.token_calls += 1
            if body.get("clientSecret") != SECRET:
                return httpx.Response(401, json={"message": "invalid secret"})
            return httpx.Response(
                200,
                json={"access_token": f"tok{self.token_calls}", "expires_in": "7200"},
            )
        if path == "/gateway":
            return httpx.Response(200, json={"url": "wss://fake/gateway"})
        if path.endswith("/files"):
            if self.fail_upload:
                return httpx.Response(
                    400, json={"code": 850012, "message": self.fail_upload}
                )
            return httpx.Response(200, json={"file_info": "FILEINFO", "ttl": 3600})
        if path.endswith("/messages"):
            if self.fail_codes:
                code = self.fail_codes.pop(0)
                return httpx.Response(
                    400, json={"code": code, "message": f"err {code}"}
                )
            if self.fail_send:
                return httpx.Response(
                    400, json={"code": 304003, "message": self.fail_send}
                )
            return httpx.Response(
                200, json={"id": f"OUT{next(self._ids)}", "timestamp": "now"}
            )
        return httpx.Response(404, json={"message": "unknown path"})


class _NoGateway:
    """Gateway stand-in for hubs driven by direct ``ingest`` calls."""

    def __init__(self, api, on_dispatch):
        self.state = "test"

    async def run(self):
        return None

    def stop(self):
        return None


@pytest.fixture
def fake_api():
    return FakeOpenAPI()


@pytest.fixture
def make_hub(fake_api, tmp_path):
    """Build hubs over the fake OpenAPI and an on-disk store under tmp."""
    hubs: list[QQHub] = []

    def _make(store_path=None, **kwargs) -> QQHub:
        creds = Credentials(APP_ID, SECRET)
        client = httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler))
        api = QQApi(
            creds,
            client,
            api_base="https://api.test",
            token_url="https://api.test/app/getAppAccessToken",
        )
        store = ChatStore(store_path or tmp_path / "chat.kvault")
        hub = QQHub(creds, api=api, store=store, gateway_factory=_NoGateway, **kwargs)
        hubs.append(hub)
        return hub

    return _make


def c2c_event(msg_id: str, openid: str, content: str, **extra) -> dict:
    """Return a ``C2C_MESSAGE_CREATE`` payload."""
    return {
        "id": msg_id,
        "author": {"user_openid": openid},
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def group_event(msg_id: str, group: str, member: str, content: str, **extra) -> dict:
    """Return a group message payload."""
    return {
        "id": msg_id,
        "group_openid": group,
        "author": {"member_openid": member},
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
