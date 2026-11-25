"""Prepare outbound text and media for ``qq_send``: splitting, loading, silk voice.

Local files and https URLs (downloaded here, never handed to QQ) are uploaded
as bytes in one request up to :data:`MAX_SINGLE_UPLOAD_BYTES`. Larger files
need QQ's chunked upload, which is not implemented; :class:`ChunkedUploader`
is the seam for it.
"""

import asyncio
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote, urlparse

import httpx

from kt_qqbot import media as inbound
from kt_qqbot.api import FILE_FILE, FILE_IMAGE, FILE_VIDEO, FILE_VOICE
from kt_qqbot.errors import MediaError

MEDIA_TYPES = {
    "image": FILE_IMAGE,
    "video": FILE_VIDEO,
    "voice": FILE_VOICE,
    "file": FILE_FILE,
}
MAX_SINGLE_UPLOAD_BYTES = 5 * 1024 * 1024
VOICE_CODEC_MISSING = (
    "voice needs silk and the voice codec is not installed; send a .silk file, "
    "or say it as text (the operator can install kt-qqbot[voice])"
)
MEDIA_HELP = "QQ takes images (png/jpg/gif/webp), mp4 video, voice and files"
# Per-kind download limits for https URLs: (max bytes, accepted content types).
URL_LIMITS: dict[str, tuple[int, tuple[str, ...]]] = {
    "image": (10 * 1024 * 1024, ("image/",)),
    "video": (20 * 1024 * 1024, ("video/",)),
    "voice": (10 * 1024 * 1024, ("audio/", "voice", "application/octet-stream")),
    "file": (20 * 1024 * 1024, ("",)),
}
URL_TIMEOUT_S = 30.0
MAX_REDIRECTS = 3
_BREAKS = ("\n\n", "\n")
_SENTENCE_END = re.compile(r"[。！？!?；;…]+[」』”\"')）]*|[.](?=\s)")

_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"},
    "voice": {".silk", ".slk", ".mp3", ".wav", ".amr", ".ogg", ".m4a", ".flac"},
}


@dataclass(frozen=True)
class MediaItem:
    """One media file ready to upload, by bytes or by https URL."""

    media_type: str
    file_type: int
    name: str
    data: bytes | None = None
    url: str | None = None
    fallback: Path | None = None


class ChunkedUploader(Protocol):
    """Upload a file too large for one request; returns its ``file_info``."""

    async def upload(
        self, kind: str, openid: str, file_type: int, data: bytes
    ) -> str: ...


def infer_type(name: str, mime: str | None = None) -> str:
    """Return image / video / voice / file from a file name, URL, or MIME type."""
    suffix = Path(urlparse(name).path).suffix.lower()
    for media_type, extensions in _EXTENSIONS.items():
        if suffix in extensions:
            return media_type
    mime = mime or mimetypes.guess_type(name)[0] or ""
    for media_type in ("image", "video"):
        if mime.startswith(f"{media_type}/"):
            return media_type
    if mime.startswith("audio/"):
        return "voice"
    return "file"


def parse_media_arg(value: Any) -> list[str]:
    """Return the list of media specs from one path / URL string or a list of them.

    A string is always one item, so the sandbox's ``path_arguments`` check sees
    exactly the paths the tool will read.
    """
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value.strip()]
    if not isinstance(value, (list, tuple)):
        raise MediaError("media must be a path, an https URL, or a list of them")
    return [str(v).strip() for v in value if str(v).strip()]


def is_silk(data: bytes) -> bool:
    """Return whether bytes are a SILK v3 stream (plain or Tencent-prefixed)."""
    return data[:10].lstrip(b"\x02").startswith(b"#!SILK_V3")


async def _encode_silk(data: bytes) -> bytes:
    """Encode audio bytes to Tencent SILK with the optional codec."""
    try:
        from graiax import silkcoder
    except ImportError as exc:
        raise MediaError(VOICE_CODEC_MISSING) from exc
    return await asyncio.to_thread(silkcoder.encode, data)


async def to_silk(data: bytes) -> bytes:
    """Return voice bytes as SILK, encoding other audio with the optional codec."""
    return data if is_silk(data) else await _encode_silk(data)


def split_text(text: str, limit: int) -> list[str]:
    """Split text longer than ``limit`` at paragraph, line, sentence or space boundaries.

    A piece is cut at the last boundary in its window that leaves it at least
    a third full; a text without one is cut hard at ``limit``.
    """
    limit = max(int(limit), 1)
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = _cut_point(rest[: limit + 1], limit)
        piece = rest[:cut].rstrip()
        if piece:
            pieces.append(piece)
        rest = rest[cut:].lstrip()
    if rest:
        pieces.append(rest)
    return pieces


def _cut_point(window: str, limit: int) -> int:
    floor = max(limit // 3, 1)
    for mark in _BREAKS:
        at = window.rfind(mark, floor, limit)
        if at >= floor:
            return at + len(mark)
    ends = [
        m.end() for m in _SENTENCE_END.finditer(window, 0, limit) if m.end() >= floor
    ]
    if ends:
        return ends[-1]
    at = window.rfind(" ", floor, limit)
    return at + 1 if at >= floor else limit


def _too_big(name: str, size: int, limit: int) -> MediaError:
    return MediaError(
        f"{name} is {size / 1048576:.1f} MB; QQ uploads are limited to "
        f"{limit / 1048576:.0f} MB here. Send a smaller file."
    )


def _suffix_kind(url: str) -> str | None:
    suffix = Path(urlparse(url).path).suffix.lower()
    for media_type, extensions in _EXTENSIONS.items():
        if suffix in extensions:
            return media_type
    return None


def _kind_of(mime: str) -> str:
    if mime.startswith("audio/"):
        return "voice"
    for media_type in ("image", "video"):
        if mime.startswith(f"{media_type}/"):
            return media_type
    return "file"


def _check_type(kind: str, mime: str, name: str) -> None:
    accepted = URL_LIMITS[kind][1]
    if mime == "text/html" or not any(mime.startswith(p) for p in accepted):
        shown = mime or "no content type"
        article = "an" if kind == "image" else "a"
        raise MediaError(
            f"{name} is {shown}, not {article} {kind}. Send a direct link to the "
            f"{kind} file; {MEDIA_HELP}."
        )


async def _fetch(
    client: httpx.AsyncClient, url: str, kind: str | None, size_limit: int | None
) -> tuple[bytes, str, str]:
    """Download ``url`` with guards; return ``(data, kind, final url)``."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            await inbound.check_public_url(current)
        except inbound.BlockedAddress as exc:
            raise MediaError(
                f"{exc}. Send a public https link, or a file in your working folder."
            ) from exc
        async with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.is_redirect and resp.headers.get("location"):
                current = str(resp.url.join(resp.headers["location"]))
                continue
            if resp.status_code >= 300:
                raise MediaError(
                    f"the link answered HTTP {resp.status_code}. Check it or send "
                    "another link."
                )
            name = _url_name(current)
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            found = kind or _kind_of(mime)
            _check_type(found, mime, name)
            cap = URL_LIMITS[found][0]
            if size_limit is not None and found != "voice":
                cap = min(cap, size_limit)
            length = resp.headers.get("content-length", "")
            if length.isdigit() and int(length) > cap:
                raise _too_big(name, int(length), cap)
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > cap:
                    raise _too_big(name, size, cap)
                chunks.append(chunk)
            return b"".join(chunks), found, current
    raise MediaError("the link redirects too many times. Send the final link.")


def _url_name(url: str) -> str:
    return unquote(Path(urlparse(url).path).name) or "download"


async def download(
    url: str,
    media_type: str | None,
    *,
    size_limit: int | None = MAX_SINGLE_UPLOAD_BYTES,
    client: httpx.AsyncClient | None = None,
    timeout: float = URL_TIMEOUT_S,
) -> MediaItem:
    """Download an https URL into a :class:`MediaItem` sent as bytes.

    Each hop must be https to a public address (redirects are followed by
    hand, at most :data:`MAX_REDIRECTS`). The content type must fit the kind,
    ``Content-Length`` and the streamed body must stay under the kind's cap in
    :data:`URL_LIMITS`, and the whole download must end within ``timeout``.
    """
    if not url.startswith("https://"):
        raise MediaError("media URLs must use https. Send an https link instead.")
    kind = media_type or _suffix_kind(url)
    own = client is None
    http = client or httpx.AsyncClient()
    try:
        data, kind, final = await asyncio.wait_for(
            _fetch(http, url, kind, size_limit), timeout
        )
    except asyncio.TimeoutError as exc:
        raise MediaError(
            f"downloading {_url_name(url)} took over {timeout:.0f} s. Send a "
            "smaller file or another link."
        ) from exc
    except httpx.HTTPError as exc:
        raise MediaError(
            f"could not download {_url_name(url)} ({type(exc).__name__}). Check "
            "the link or send another one."
        ) from exc
    finally:
        if own:
            await http.aclose()
    if kind == "voice" and not is_silk(data):
        data = await _encode_silk(data)
    item = MediaItem(kind, MEDIA_TYPES[kind], _url_name(final), data=data)
    if size_limit is not None:
        check_size(item, size_limit)
    return item


def check_size(item: MediaItem, limit: int = MAX_SINGLE_UPLOAD_BYTES) -> None:
    """Raise when bytes exceed what one upload request can carry."""
    if item.data is not None and len(item.data) > limit:
        raise _too_big(item.name, len(item.data), limit)


async def prepare(
    spec: str,
    media_type: str | None,
    resolve: Callable[[str], Path],
    *,
    size_limit: int | None = MAX_SINGLE_UPLOAD_BYTES,
    client: httpx.AsyncClient | None = None,
) -> MediaItem:
    """Turn a path or https URL into a :class:`MediaItem` carrying bytes."""
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise MediaError(
            f"unknown media_type {media_type!r}; use image, video, voice or file"
        )
    if spec.startswith("http://"):
        raise MediaError("media URLs must use https. Send an https link instead.")
    if spec.startswith("https://"):
        return await download(spec, media_type, size_limit=size_limit, client=client)
    path = resolve(spec)
    if not path.is_file():
        raise MediaError(
            f"media file not found: {spec}. Check the path; it is relative to "
            "your working folder."
        )
    kind = media_type or infer_type(path.name)
    size = path.stat().st_size
    if size_limit is not None and kind != "voice" and size > size_limit:
        raise _too_big(path.name, size, size_limit)
    data = path.read_bytes()
    if kind == "voice" and not is_silk(data):
        data = await _encode_silk(data)
    item = MediaItem(kind, MEDIA_TYPES[kind], path.name, data=data)
    if size_limit is not None:
        check_size(item, size_limit)
    return item
