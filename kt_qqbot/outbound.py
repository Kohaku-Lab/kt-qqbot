"""Prepare outbound media for ``qq_send``: type inference, loading, silk voice.

Local files are uploaded as bytes in one request up to
:data:`MAX_SINGLE_UPLOAD_BYTES`. Larger files need QQ's chunked upload, which
is not implemented; :class:`ChunkedUploader` is the seam for it.
"""

import asyncio
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from kt_qqbot.api import FILE_FILE, FILE_IMAGE, FILE_VIDEO, FILE_VOICE
from kt_qqbot.errors import MediaError

MEDIA_TYPES = {
    "image": FILE_IMAGE,
    "video": FILE_VIDEO,
    "voice": FILE_VOICE,
    "file": FILE_FILE,
}
MAX_SINGLE_UPLOAD_BYTES = 5 * 1024 * 1024
VOICE_CODEC_MISSING = "voice needs silk; install kt-qqbot[voice]"

_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "video": {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"},
    "voice": {".silk", ".slk", ".mp3", ".wav", ".amr", ".ogg", ".m4a", ".flac"},
}
_SILK_EXTENSIONS = {".silk", ".slk"}


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


def _too_big(name: str, size: int, limit: int) -> MediaError:
    return MediaError(
        f"{name} is {size / 1048576:.1f} MB; single uploads are limited to "
        f"{limit / 1048576:.0f} MB and chunked upload is not supported yet"
    )


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
) -> MediaItem:
    """Turn a path or https URL into a :class:`MediaItem`."""
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise MediaError(
            f"unknown media_type {media_type!r}; use image, video, voice or file"
        )
    if spec.startswith("http://"):
        raise MediaError("media URLs must use https")
    if spec.startswith("https://"):
        kind = media_type or infer_type(spec)
        suffix = Path(urlparse(spec).path).suffix.lower()
        if kind == "voice" and suffix not in _SILK_EXTENSIONS:
            raise MediaError("voice URLs must point to a .silk file; send a path")
        name = Path(urlparse(spec).path).name or spec
        return MediaItem(kind, MEDIA_TYPES[kind], name, url=spec)
    path = resolve(spec)
    if not path.is_file():
        raise MediaError(f"media file not found: {spec}")
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
