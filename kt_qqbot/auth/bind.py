"""QR-code bind flow that provisions a bot's AppID / AppSecret.

:func:`bind_flow` is an async iterator of :class:`BindStep` values, so a
terminal and a future web settings page can drive the same flow.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

import httpx

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.auth.crypto import decrypt_secret, encode_key, new_key
from kt_qqbot.errors import BindError

logger = get_logger("kt_qqbot.bind")

BIND_HOST = "https://q.qq.com"
CONNECT_PAGE = "https://q.qq.com/qqbot/openclaw/connect.html"
SOURCE = "kohakuterrarium"
POLL_INTERVAL_S = 2.0
REQUEST_TIMEOUT_S = 10.0

STATUS_NONE = 0
STATUS_PENDING = 1
STATUS_COMPLETED = 2
STATUS_EXPIRED = 3


@dataclass(frozen=True)
class BindStep:
    """One observable step of the bind flow.

    ``show_qr`` carries ``url`` (the text to encode in the QR code);
    ``done`` carries ``app_id``. The secret never appears in a step.
    """

    kind: Literal["show_qr", "progress", "done", "error"]
    url: str = ""
    message: str = ""
    app_id: str = ""


def connect_url(task_id: str, source: str = SOURCE) -> str:
    """Return the page URL a QR code encodes for ``task_id``."""
    query = urlencode({"task_id": task_id, "source": source, "_wv": "2"})
    return f"{CONNECT_PAGE}?{query}"


async def create_task(client: httpx.AsyncClient, key: bytes) -> str:
    """Register a bind task for ``key``; return its task id."""
    resp = await client.post(
        f"{BIND_HOST}/lite/create_bind_task",
        json={"key": encode_key(key)},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    body = resp.json()
    task_id = (body.get("data") or {}).get("task_id")
    if body.get("retcode") != 0 or not task_id:
        raise BindError(f"create_bind_task failed: {body}")
    return str(task_id)


async def poll_task(client: httpx.AsyncClient, task_id: str) -> dict:
    """Return the ``data`` object of one ``poll_bind_result`` call."""
    resp = await client.post(
        f"{BIND_HOST}/lite/poll_bind_result",
        json={"task_id": task_id},
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("retcode") != 0:
        raise BindError(f"poll_bind_result failed: {body}")
    return body.get("data") or {}


async def bind_flow(
    *,
    client: httpx.AsyncClient | None = None,
    store: CredentialStore | None = None,
    source: str = SOURCE,
    poll_interval: float = POLL_INTERVAL_S,
    timeout: float | None = None,
) -> AsyncIterator[BindStep]:
    """Run the bind protocol, saving the credentials on success.

    An expired QR code is replaced by a new one. Transient poll errors are
    retried. ``timeout`` (seconds) ends the flow with an ``error`` step.
    """
    own_client = client is None
    client = client or httpx.AsyncClient()
    store = store or CredentialStore()
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            key = new_key()
            try:
                task_id = await create_task(client, key)
            except (httpx.HTTPError, BindError, ValueError) as exc:
                yield BindStep("error", message=f"Could not start binding: {exc}")
                return
            yield BindStep("show_qr", url=connect_url(task_id, source))
            outcome = None
            async for step in _poll_until_final(
                client, task_id, key, store, poll_interval, deadline
            ):
                if step.kind in ("done", "error"):
                    outcome = step
                yield step
            if outcome is not None:
                return
            yield BindStep("progress", message="QR code expired; showing a new one.")
    finally:
        if own_client:
            await client.aclose()


async def _poll_until_final(
    client: httpx.AsyncClient,
    task_id: str,
    key: bytes,
    store: CredentialStore,
    poll_interval: float,
    deadline: float | None,
) -> AsyncIterator[BindStep]:
    """Poll one task; end on done / error, or silently when it expires."""
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            yield BindStep("error", message="Binding timed out.")
            return
        await asyncio.sleep(poll_interval)
        try:
            data = await poll_task(client, task_id)
        except (httpx.HTTPError, BindError, ValueError) as exc:
            logger.debug("Bind poll failed; retrying", error=str(exc))
            continue
        status = data.get("status")
        if status == STATUS_EXPIRED:
            return
        if status != STATUS_COMPLETED:
            continue
        app_id = str(data.get("bot_appid") or "")
        blob = data.get("bot_encrypt_secret") or ""
        if not app_id or not blob:
            yield BindStep("error", message="Bind result is missing the bot id.")
            return
        try:
            secret = decrypt_secret(blob, key)
        except BindError as exc:
            yield BindStep("error", message=str(exc))
            return
        store.save(Credentials(app_id, secret))
        logger.info("Bot bound", app_id=app_id)
        yield BindStep("done", app_id=app_id, message=f"Bound bot {app_id}.")
        return
