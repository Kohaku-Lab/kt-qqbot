"""Token cache and REST client against the fake OpenAPI."""

import asyncio

import httpx
import pytest

from kt_qqbot.api import MSG_MEDIA, QQApi, TokenCache
from kt_qqbot.auth.credentials import Credentials
from kt_qqbot.errors import AuthFailed, QQApiError

from tests.conftest import APP_ID, SECRET

TOKEN_URL = "https://api.test/app/getAppAccessToken"


def _client(fake_api):
    return httpx.AsyncClient(transport=httpx.MockTransport(fake_api.handler))


class TestTokenCache:
    async def test_caches_and_refreshes_before_expiry(self, fake_api):
        now = [1000.0]
        async with _client(fake_api) as client:
            cache = TokenCache(
                Credentials(APP_ID, SECRET),
                client,
                token_url=TOKEN_URL,
                clock=lambda: now[0],
            )
            assert await cache.get() == "tok1"
            now[0] += 7200 - 301
            assert await cache.get() == "tok1"
            now[0] += 2
            assert await cache.get() == "tok2"
            cache.invalidate()
            assert await cache.get() == "tok3"
        assert fake_api.requests[0][2] == {"appId": APP_ID, "clientSecret": SECRET}

    async def test_concurrent_callers_share_one_fetch(self, fake_api):
        async with _client(fake_api) as client:
            cache = TokenCache(Credentials(APP_ID, SECRET), client, token_url=TOKEN_URL)
            tokens = await asyncio.gather(*(cache.get() for _ in range(5)))
        assert tokens == ["tok1"] * 5
        assert fake_api.token_calls == 1

    async def test_rejected_secret_raises_auth_failed(self, fake_api):
        async with _client(fake_api) as client:
            cache = TokenCache(
                Credentials(APP_ID, "wrong"), client, token_url=TOKEN_URL
            )
            with pytest.raises(AuthFailed, match="kt-qqbot login"):
                await cache.get()


class TestQQApi:
    def _api(self, fake_api):
        return QQApi(
            Credentials(APP_ID, SECRET),
            _client(fake_api),
            api_base="https://api.test",
            token_url=TOKEN_URL,
        )

    async def test_send_payload_and_headers(self, fake_api):
        api = self._api(fake_api)
        await api.send_message(
            "group", "G1", content="hi", msg_id="M1", msg_seq=2, reference_id="M0"
        )
        await api.send_message(
            "c2c", "U1", content="", msg_type=MSG_MEDIA, media_file_info="F"
        )
        await api.aclose()
        group = [r for r in fake_api.requests if r[1] == "/v2/groups/G1/messages"]
        assert group[0][2] == {
            "msg_type": 0,
            "content": "hi",
            "msg_id": "M1",
            "msg_seq": 2,
            "message_reference": {"message_id": "M0"},
        }
        user = [r for r in fake_api.requests if r[1] == "/v2/users/U1/messages"]
        assert user[0][2]["media"] == {"file_info": "F"}
        assert "msg_id" not in user[0][2]

    async def test_error_body_raises_typed_error(self, fake_api):
        fake_api.fail_send = "msg_id expired"
        api = self._api(fake_api)
        with pytest.raises(QQApiError) as info:
            await api.send_message("c2c", "U1", content="x")
        await api.aclose()
        assert info.value.code == 40054005
        assert info.value.message == "msg_id expired"

    async def test_401_retries_once_with_new_token(self, fake_api):
        seen = []

        def handler(request):
            if request.url.path == "/app/getAppAccessToken":
                return fake_api.handler(request)
            seen.append(request.headers["Authorization"])
            if len(seen) == 1:
                return httpx.Response(401, json={"message": "expired"})
            return httpx.Response(200, json={"url": "wss://x"})

        api = QQApi(
            Credentials(APP_ID, SECRET),
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            api_base="https://api.test",
            token_url=TOKEN_URL,
        )
        assert await api.gateway_url() == "wss://x"
        await api.aclose()
        assert seen == ["QQBot tok1", "QQBot tok2"]

    async def test_upload_media_by_bytes(self, fake_api):
        api = self._api(fake_api)
        info = await api.upload_media("group", "G1", 1, data=b"\x89PNG")
        await api.aclose()
        assert info == "FILEINFO"
        body = fake_api.sent("/files")[0]
        assert body["file_type"] == 1 and body["file_data"] == "iVBORw=="
