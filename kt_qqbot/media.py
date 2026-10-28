"""Inbound image download, with a public-address guard."""

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx

from kohakuterrarium.llm.message import ImagePart
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.paths import media_dir

logger = get_logger("kt_qqbot.media")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 30.0
IMAGE_TIMEOUT_S = 5.0
MAX_CACHED_FILES = 500


@dataclass
class ImageOptions:
    """Limits for turning inbound images into vision input."""

    enabled: bool = True
    detail: Literal["auto", "low", "high"] = "auto"
    max_per_delivery: int = 8
    max_bytes: int = MAX_IMAGE_BYTES
    timeout: float = IMAGE_TIMEOUT_S


class BlockedAddress(ValueError):
    """The URL resolves to an address that must not be fetched."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


async def check_public_url(url: str) -> None:
    """Raise :class:`BlockedAddress` unless ``url`` is HTTPS to globally routable hosts.

    The check resolves the host once; the download resolves it again, so a
    DNS-rebinding host could still differ between the two (attachment URLs
    come from QQ, which limits the exposure).
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise BlockedAddress(f"only https URLs are fetched: {url}")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise BlockedAddress(f"cannot resolve {parsed.hostname}: {exc}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_is_public(a) for a in addresses):
        raise BlockedAddress(f"{parsed.hostname} resolves to a non-public address")


async def fetch_image(
    client: httpx.AsyncClient, url: str, *, max_bytes: int = MAX_IMAGE_BYTES
) -> tuple[bytes, str]:
    """Download one image; return ``(data, mime)``."""
    await check_public_url(url)
    async with client.stream(
        "GET", url, timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=False
    ) as resp:
        resp.raise_for_status()
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"image larger than {max_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks), mime


def _cache_file(app_id: str, msg_id: str, index: int) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in msg_id)
    return media_dir() / app_id / f"{safe}.{index}"


def cache_attachment(app_id: str, msg_id: str, index: int, data: bytes) -> Path:
    """Keep a downloaded attachment so it can be resent later (bounded count)."""
    path = _cache_file(app_id, msg_id, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    files = sorted(path.parent.iterdir(), key=lambda p: p.stat().st_mtime)
    for old in files[:-MAX_CACHED_FILES]:
        old.unlink(missing_ok=True)
    return path


def cached_attachment(app_id: str, msg_id: str, index: int) -> Path | None:
    """Return the cached copy of an attachment, if one was kept."""
    path = _cache_file(app_id, msg_id, index)
    return path if path.is_file() else None


async def images_for(
    client: httpx.AsyncClient,
    messages: list[dict],
    options: ImageOptions,
    cache_app: str | None = None,
) -> list[ImagePart]:
    """Download the image attachments of ``messages`` as data-URL image parts.

    With ``cache_app`` each downloaded image is also kept for resending.
    """
    if not options.enabled:
        return []
    jobs: list[tuple[dict, int, str]] = []
    for msg in messages:
        for index, att in enumerate(msg.get("attachments") or [], start=1):
            if str(att.get("content_type", "")).startswith("image/"):
                jobs.append((msg, index, str(att.get("url") or "")))
    jobs = jobs[: options.max_per_delivery]

    async def one(msg: dict, index: int, url: str) -> ImagePart | None:
        try:
            data, mime = await asyncio.wait_for(
                fetch_image(client, url, max_bytes=options.max_bytes),
                timeout=options.timeout,
            )
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            logger.info("Image skipped", url=url, error=str(exc) or type(exc).__name__)
            return None
        if cache_app and msg.get("msg_id"):
            try:
                cache_attachment(cache_app, str(msg["msg_id"]), index, data)
            except OSError as exc:
                logger.debug("Attachment not cached", error=str(exc))
        encoded = base64.b64encode(data).decode("ascii")
        return ImagePart(
            url=f"data:{mime};base64,{encoded}",
            detail=options.detail,
            source_type="qq",
            source_name=f"m{msg.get('seq')}.{index}",
        )

    results = await asyncio.gather(*(one(*job) for job in jobs))
    return [part for part in results if part is not None]
