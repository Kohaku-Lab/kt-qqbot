"""The persona image: ``<config>/qq-bot/character.(png|jpg|jpeg|webp)``.

:func:`prepare_persona` downscales it once to at most :data:`MAX_SIDE` pixels
on the long side and caches the result by file hash.
"""

import base64
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from kohakuterrarium.llm.message import ContentPart, ImagePart, TextPart

PERSONA_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MAX_SIDE = 768
PERSONA_NOTE = (
    "[reference image] This is your own appearance as the character. "
    "It is not a chat message."
)
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


@dataclass(frozen=True)
class PreparedPersona:
    """The persona file's original size and its downscaled bytes."""

    path: Path
    width: int
    height: int
    bytes: int
    mime: str
    data: bytes


def find_persona(directory: Path) -> tuple[Path | None, list[Path]]:
    """Return the persona file (first by extension order) and the ignored others."""
    found = [
        directory / f"character{ext}"
        for ext in PERSONA_EXTENSIONS
        if (directory / f"character{ext}").is_file()
    ]
    return (found[0], found[1:]) if found else (None, [])


def prepare_persona(path: Path, cache_dir: Path) -> PreparedPersona:
    """Load, downscale (cached by content hash) and describe the persona image."""
    raw = path.read_bytes()
    mime = _MIME[path.suffix.lower()]
    digest = hashlib.sha256(raw).hexdigest()[:32]
    cached = cache_dir / f"persona-{digest}{path.suffix.lower()}"
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        if cached.is_file():
            data = cached.read_bytes()
        else:
            if max(width, height) > MAX_SIDE:
                image = image.copy()
                image.thumbnail((MAX_SIDE, MAX_SIDE))
                buffer = io.BytesIO()
                fmt = _FORMAT[mime]
                if fmt == "JPEG" and image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                image.save(buffer, format=fmt)
                data = buffer.getvalue()
            else:
                data = raw
            cache_dir.mkdir(parents=True, exist_ok=True)
            for old in cache_dir.glob("persona-*"):
                old.unlink(missing_ok=True)
            cached.write_bytes(data)
    return PreparedPersona(path, width, height, len(raw), mime, data)


def persona_content(prepared: PreparedPersona) -> list[ContentPart]:
    """Return the pinned-message content: a note plus the low-detail image."""
    encoded = base64.b64encode(prepared.data).decode("ascii")
    return [
        TextPart(text=PERSONA_NOTE),
        ImagePart(
            url=f"data:{prepared.mime};base64,{encoded}",
            detail="low",
            source_type="persona",
            source_name=prepared.path.name,
        ),
    ]
