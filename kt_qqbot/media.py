"""Inbound image download, with a public-address guard."""

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from kohakuterrarium.llm.message import ImagePart
from kohakuterrarium.utils.logging import get_logger

logger = get_logger("kt_qqbot.media")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 30.0


@dataclass
class ImageOptions:
    """Limits for turning inbound images into vision input."""

    enabled: bool = True
    detail: Literal["auto", "low", "high"] = "auto"
    max_per_delivery: int = 8
    max_bytes: int = MAX_IMAGE_BYTES


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


async def images_for(
    client: httpx.AsyncClient, messages: list[dict], options: ImageOptions
) -> list[ImagePart]:
    """Download the image attachments of ``messages`` as data-URL image parts."""
    if not options.enabled:
        return []
    parts: list[ImagePart] = []
    for msg in messages:
        for att in msg.get("attachments") or []:
            if len(parts) >= options.max_per_delivery:
                return parts
            if not str(att.get("content_type", "")).startswith("image/"):
                continue
            try:
                data, mime = await fetch_image(
                    client, att["url"], max_bytes=options.max_bytes
                )
            except (httpx.HTTPError, ValueError) as exc:
                logger.info("Image skipped", url=att.get("url"), error=str(exc))
                continue
            encoded = base64.b64encode(data).decode("ascii")
            parts.append(
                ImagePart(
                    url=f"data:{mime};base64,{encoded}",
                    detail=options.detail,
                    source_type="qq",
                    source_name=f"m{msg.get('seq')} {att.get('filename') or 'image'}",
                )
            )
    return parts
