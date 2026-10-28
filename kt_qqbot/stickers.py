"""A user-curated sticker folder: search by file name and optional tags.

Folder: ``<config>/qq-bot/stickers/`` (any depth). ``tags.yaml`` may map a
relative file path to a list of tags, a mood string, or
``{tags: [...], mood: "..."}``.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.paths import root_dir

logger = get_logger("kt_qqbot.stickers")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_WORDS = re.compile(r"[^\w]+|_", re.UNICODE)


@dataclass
class Sticker:
    """One sticker file and the words it can be found by."""

    name: str
    path: Path
    words: set[str] = field(default_factory=set)
    mood: str = ""


def _words(text: str) -> set[str]:
    return {w for w in _WORDS.split(text.lower()) if w}


class StickerLibrary:
    """Search and resolve stickers inside one folder."""

    def __init__(self, directory: Path | str | None = None):
        self.directory = (
            Path(directory).expanduser() if directory else root_dir() / "stickers"
        )

    def _tags(self) -> dict:
        file = self.directory / "tags.yaml"
        if not file.is_file():
            return {}
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            logger.warning("tags.yaml unreadable", error=str(exc))
            return {}
        return data if isinstance(data, dict) else {}

    def all(self) -> list[Sticker]:
        """Return every sticker in the folder, sorted by name."""
        if not self.directory.is_dir():
            return []
        tags = self._tags()
        root = self.directory.resolve()
        stickers = []
        for path in sorted(self.directory.rglob("*")):
            if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
                continue
            if not path.resolve().is_relative_to(root):
                continue
            name = path.relative_to(self.directory).as_posix()
            sticker = Sticker(name, path, _words(Path(name).with_suffix("").as_posix()))
            entry = tags.get(name)
            if isinstance(entry, str):
                sticker.mood = entry
            elif isinstance(entry, list):
                sticker.words |= {str(t).lower() for t in entry}
            elif isinstance(entry, dict):
                sticker.words |= {str(t).lower() for t in entry.get("tags") or []}
                sticker.mood = str(entry.get("mood") or "")
            sticker.words |= _words(sticker.mood)
            stickers.append(sticker)
        return stickers

    def search(self, query: str, limit: int = 4) -> list[Sticker]:
        """Return up to ``limit`` stickers, best word overlap first."""
        wanted = _words(query)
        stickers = self.all()
        if not wanted:
            return stickers[:limit]
        scored = []
        for sticker in stickers:
            hits = sum(
                1
                for w in wanted
                if any(w in word or word in w for word in sticker.words)
            )
            if hits:
                scored.append((-hits, sticker.name, sticker))
        return [s for _, _, s in sorted(scored)[:limit]]

    def resolve(self, name: str) -> Path:
        """Return the file of ``sticker:<name>``; refuse anything outside the folder."""
        root = self.directory.resolve()
        path = (self.directory / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"sticker {name!r} is outside the sticker folder")
        if path.suffix.lower() not in IMAGE_EXTENSIONS or not path.is_file():
            raise ValueError(f"no sticker named {name!r}")
        return path
