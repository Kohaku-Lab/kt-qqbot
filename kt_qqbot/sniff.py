"""Recognise a file's real format from its first bytes, for media sent as bytes."""

from dataclasses import dataclass

from kt_qqbot.errors import MediaError
from kt_qqbot.outbound import MEDIA_HELP, is_silk

SNIFF_BYTES = 64


@dataclass(frozen=True)
class Sniffed:
    """A recognised format: the media kind it belongs to and a short label."""

    kind: str
    label: str


# (offset, magic bytes, kind, label); the first match wins.
_SIGNATURES: tuple[tuple[int, bytes, str, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "image", "a PNG image"),
    (0, b"\xff\xd8\xff", "image", "a JPEG image"),
    (0, b"GIF87a", "image", "a GIF image"),
    (0, b"GIF89a", "image", "a GIF image"),
    (0, b"BM", "image", "a BMP image"),
    (0, b"\x1a\x45\xdf\xa3", "video", "a WebM/MKV video"),
    (0, b"OggS", "voice", "OGG audio"),
    (0, b"fLaC", "voice", "FLAC audio"),
    (0, b"ID3", "voice", "MP3 audio"),
    (0, b"#!AMR", "voice", "AMR audio"),
    (0, b"%PDF-", "file", "a PDF document"),
    (0, b"PK\x03\x04", "file", "a zip archive"),
)
_RIFF = {b"WEBP": ("image", "a WebP image"), b"AVI ": ("video", "an AVI video")}
_RIFF_AUDIO = {b"WAVE": ("voice", "WAV audio")}
_AUDIO_BRANDS = (b"M4A ", b"M4B ")
_MP3_FRAMES = (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


def sniff(data: bytes) -> Sniffed | None:
    """Return the format the bytes start with, ``text`` for plain text, or None."""
    head = data[:SNIFF_BYTES]
    if is_silk(head):
        return Sniffed("voice", "SILK voice")
    if head[:4] == b"RIFF" and len(head) >= 12:
        found = _RIFF.get(head[8:12]) or _RIFF_AUDIO.get(head[8:12])
        if found:
            return Sniffed(*found)
    if head[4:8] == b"ftyp":
        if head[8:12] in _AUDIO_BRANDS:
            return Sniffed("voice", "M4A audio")
        return Sniffed("video", "an MP4/MOV video")
    for offset, magic, kind, label in _SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return Sniffed(kind, label)
    if head[:2] in _MP3_FRAMES:
        return Sniffed("voice", "MP3 audio")
    if _looks_like_text(head):
        return Sniffed("text", "text")
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.reason == "unexpected end of data"
    return True


def check_kind(name: str, kind: str, data: bytes) -> None:
    """Raise when bytes sent as ``kind`` are really something else.

    Images and videos must be a format of that kind; voice is refused only
    when it is clearly another kind (audio QQ cannot take is converted later).
    Files are never refused.
    """
    if kind == "file":
        return
    found = sniff(data)
    if found is not None and found.kind == kind:
        return
    if kind == "voice" and found is None:
        return
    article = "an" if kind == "image" else "a"
    if found is None:
        raise MediaError(
            f"{name} is not {article} {kind} QQ can show (unknown format); "
            f"{MEDIA_HELP}. Check the file, or send it with media_type file."
        )
    target = found.kind if found.kind in ("image", "video", "voice") else "file"
    raise MediaError(
        f"{name} is not {article} {kind} (it looks like {found.label}). Check the "
        f"file, or send it with media_type {target}."
    )
