"""QQ OpenAPI client: access-token cache plus the REST calls the bot uses."""

import asyncio
import base64
import time
from collections.abc import Callable
from typing import Any

import httpx

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.auth.credentials import Credentials
from kt_qqbot.errors import AuthFailed, QQApiError

logger = get_logger("kt_qqbot.api")

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
API_BASE = "https://api.bot.qq.com"
DEFAULT_EXPIRES_S = 7200
# A token is refreshed this long before it expires (capped at a third of its life).
REFRESH_MARGIN_S = 300

MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_MEDIA = 7
MSG_INPUT_NOTIFY = 6

FILE_IMAGE = 1
FILE_VIDEO = 2
FILE_VOICE = 3
FILE_FILE = 4


class TokenCache:
    """Fetch and cache one bot's access token; at most one fetch runs at a time."""

    def __init__(
        self,
        creds: Credentials,
        client: httpx.AsyncClient,
        *,
        token_url: str = TOKEN_URL,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.creds = creds
        self.client = client
        self.token_url = token_url
        self._clock = clock
        self._token: str | None = None
        self._refresh_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Forget the cached token so the next call fetches a new one."""
        self._token = None
        self._refresh_at = 0.0

    async def get(self) -> str:
        """Return a valid access token."""
        if self._token and self._clock() < self._refresh_at:
            return self._token
        async with self._lock:
            if self._token and self._clock() < self._refresh_at:
                return self._token
            await self._fetch()
            return self._token or ""

    async def _fetch(self) -> None:
        try:
            resp = await self.client.post(
                self.token_url,
                json={
                    "appId": self.creds.app_id,
                    "clientSecret": self.creds.app_secret,
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise QQApiError(0, None, f"token request failed: {exc}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        token = body.get("access_token")
        if resp.status_code >= 400 or not token:
            detail = (
                body.get("message") or resp.text[:200] or f"HTTP {resp.status_code}"
            )
            raise AuthFailed(self.creds.app_id, str(detail))
        try:
            expires = int(body.get("expires_in") or DEFAULT_EXPIRES_S)
        except (TypeError, ValueError):
            expires = DEFAULT_EXPIRES_S
        margin = min(REFRESH_MARGIN_S, expires / 3)
        self._token = str(token)
        self._refresh_at = self._clock() + max(expires - margin, 1)
        logger.debug("Access token refreshed", app_id=self.creds.app_id)


async def check_credentials(
    creds: Credentials, client: httpx.AsyncClient | None = None
) -> None:
    """Fetch one token to prove ``creds`` work; raise :class:`AuthFailed` if not."""
    if client is not None:
        await TokenCache(creds, client).get()
        return
    async with httpx.AsyncClient() as own:
        await TokenCache(creds, own).get()


class QQApi:
    """REST calls against the QQ OpenAPI for one bot."""

    def __init__(
        self,
        creds: Credentials,
        client: httpx.AsyncClient | None = None,
        *,
        api_base: str = API_BASE,
        token_url: str = TOKEN_URL,
    ):
        self.creds = creds
        self.client = client or httpx.AsyncClient()
        self.api_base = api_base.rstrip("/")
        self.tokens = TokenCache(creds, self.client, token_url=token_url)

    def use_credentials(self, creds: Credentials) -> None:
        """Switch to new credentials; the next call fetches a new token."""
        self.creds = creds
        self.tokens = TokenCache(creds, self.client, token_url=self.tokens.token_url)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def _headers(self) -> dict[str, str]:
        token = await self.tokens.get()
        return {
            "Authorization": f"QQBot {token}",
            "X-Union-Appid": self.creds.app_id,
        }

    async def request(self, method: str, path: str, json: Any = None) -> dict:
        """Call ``path``; retry once with a new token on HTTP 401."""
        for attempt in (0, 1):
            headers = await self._headers()
            try:
                resp = await self.client.request(
                    method,
                    f"{self.api_base}{path}",
                    json=json,
                    headers=headers,
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                raise QQApiError(0, None, str(exc)) from exc
            if resp.status_code == 401 and attempt == 0:
                self.tokens.invalidate()
                continue
            try:
                body = resp.json() if resp.content else {}
            except ValueError:
                body = {"message": resp.text[:200]}
            if resp.status_code >= 400 or (
                isinstance(body, dict) and body.get("code") not in (None, 0)
            ):
                code = body.get("code") if isinstance(body, dict) else None
                message = (
                    body.get("message", "") if isinstance(body, dict) else str(body)
                )
                raise QQApiError(resp.status_code, code, message)
            return body if isinstance(body, dict) else {"data": body}
        raise QQApiError(401, None, "unauthorized")

    async def gateway_url(self) -> str:
        """Return the WebSocket gateway URL."""
        body = await self.request("GET", "/gateway")
        url = body.get("url")
        if not url:
            raise QQApiError(200, None, f"gateway response has no url: {body}")
        return str(url)

    @staticmethod
    def _messages_path(kind: str, openid: str) -> str:
        scope = "groups" if kind == "group" else "users"
        return f"/v2/{scope}/{openid}/messages"

    @staticmethod
    def _files_path(kind: str, openid: str) -> str:
        scope = "groups" if kind == "group" else "users"
        return f"/v2/{scope}/{openid}/files"

    async def send_message(
        self,
        kind: str,
        openid: str,
        *,
        content: str = "",
        msg_type: int = MSG_TEXT,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        reference_id: str | None = None,
        media_file_info: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """Send one message to a C2C user or a group."""
        payload: dict[str, Any] = {"msg_type": msg_type, "content": content}
        if msg_id:
            payload["msg_id"] = msg_id
        if msg_seq is not None:
            payload["msg_seq"] = msg_seq
        if reference_id:
            payload["message_reference"] = {"message_id": reference_id}
        if media_file_info:
            payload["media"] = {"file_info": media_file_info}
        if extra:
            payload.update(extra)
        return await self.request(
            "POST", self._messages_path(kind, openid), json=payload
        )

    async def upload_media(
        self,
        kind: str,
        openid: str,
        file_type: int,
        *,
        url: str | None = None,
        data: bytes | None = None,
    ) -> str:
        """Upload one media file by URL or bytes; return its ``file_info``.

        ``file_type`` is 1 image, 2 video, 3 voice (silk), 4 file.
        """
        payload: dict[str, Any] = {"file_type": file_type, "srv_send_msg": False}
        if url:
            payload["url"] = url
        elif data is not None:
            payload["file_data"] = base64.b64encode(data).decode("ascii")
        else:
            raise ValueError("upload_media needs url or data")
        body = await self.request("POST", self._files_path(kind, openid), json=payload)
        info = body.get("file_info")
        if not info:
            raise QQApiError(200, None, f"upload response has no file_info: {body}")
        return str(info)
