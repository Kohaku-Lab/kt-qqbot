"""Load ``sandbox:<sandbox>/<path>`` media handles through the optional kt-sandbox."""

import asyncio

from kt_qqbot.errors import MediaError
from kt_qqbot.outbound import (
    MEDIA_TYPES,
    URL_LIMITS,
    MediaItem,
    check_size,
    infer_type,
    to_silk,
)
from kt_qqbot.sniff import check_kind

try:
    from kt_sandbox import files as sandbox_files
    from kt_sandbox.errors import SandboxError
except ImportError:
    sandbox_files = None
    SandboxError = None

PREFIX = "sandbox:"
NOT_INSTALLED = (
    "sandbox files need the kt-sandbox package; install it with: pip install "
    '"kt-sandbox[manager] @ git+https://github.com/Kohaku-Lab/kt-sandbox"'
)


def is_sandbox_ref(spec: str) -> bool:
    """Return whether a media spec names a file inside a sandbox."""
    return spec.startswith(PREFIX)


async def load(spec: str, media_type: str | None, size_limit: int | None) -> MediaItem:
    """Read a sandbox file into a :class:`MediaItem` carrying its bytes.

    The read is capped at ``size_limit`` (voice, which is converted after
    reading, and an unlimited upload use the per-kind cap in ``URL_LIMITS``).
    The bytes must really be the kind they are sent as.
    """
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise MediaError(
            f"unknown media_type {media_type!r}; use image, video, voice or file"
        )
    if sandbox_files is None:
        raise MediaError(NOT_INSTALLED)
    try:
        _, path = sandbox_files.parse_ref(spec)
        kind = media_type or infer_type(path)
        cap = URL_LIMITS[kind][0]
        if size_limit is not None and kind != "voice":
            cap = min(cap, size_limit)
        found = await asyncio.to_thread(sandbox_files.read, spec, max_bytes=cap)
    except SandboxError as exc:
        raise MediaError(str(exc)) from exc
    check_kind(found.name, kind, found.data)
    data = await to_silk(found.data) if kind == "voice" else found.data
    item = MediaItem(kind, MEDIA_TYPES[kind], found.name, data=data)
    if size_limit is not None:
        check_size(item, size_limit)
    return item
