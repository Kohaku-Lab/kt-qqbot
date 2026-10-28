"""Checks against the real QQ platform; run with KT_QQBOT_LIVE=1 after `kt-qqbot login`."""

import os

import httpx
import pytest

from kt_qqbot.api import QQApi, TokenCache
from kt_qqbot.auth.credentials import CredentialStore

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("KT_QQBOT_LIVE") != "1", reason="set KT_QQBOT_LIVE=1"
    ),
]


@pytest.fixture(autouse=True)
def isolated_config():
    """Live tests read the real credential store."""
    yield


async def test_token_and_gateway_url():
    creds = CredentialStore().load()
    async with httpx.AsyncClient() as client:
        assert await TokenCache(creds, client).get()
        url = await QQApi(creds, client).gateway_url()
    assert url.startswith("wss://")
