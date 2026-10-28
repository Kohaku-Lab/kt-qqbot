"""``qq_character`` plugin: add the user's character files to the system prompt.

Files come from ``<config>/qq-bot/``: ``character.md`` first, then every
``*.md`` under ``character/`` in name order. Missing files add nothing.
"""

from pathlib import Path

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.paths import root_dir

logger = get_logger("kt_qqbot.character")

MAX_CHARS = 20000


class QQCharacterPlugin(BasePlugin):
    """Inject user-authored character files as a ``## Character`` prompt section."""

    name = "qq_character"
    priority = 40
    description = (
        "Add the character files from ~/.kohakuterrarium/qq-bot to the prompt."
    )

    def __init__(
        self,
        directory: str | None = None,
        heading: str = "Character",
        max_chars: int = MAX_CHARS,
    ):
        super().__init__()
        self.directory = Path(directory).expanduser() if directory else None
        self.heading = heading
        self.max_chars = int(max_chars)

    def files(self) -> list[Path]:
        """Return the character files that exist, in prompt order."""
        base = self.directory or root_dir()
        found = []
        main = base / "character.md"
        if main.is_file():
            found.append(main)
        extra = base / "character"
        if extra.is_dir():
            found.extend(sorted(p for p in extra.glob("*.md") if p.is_file()))
        return found

    def get_prompt_content(self, context: PluginContext) -> str | None:
        """Return the character section, or None when no file has content."""
        parts = []
        for path in self.files():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning(
                    "Character file unreadable", path=str(path), error=str(exc)
                )
                continue
            if text:
                parts.append(text)
        if not parts:
            return None
        body = "\n\n".join(parts)
        if len(body) > self.max_chars:
            logger.warning(
                "Character files truncated", chars=len(body), limit=self.max_chars
            )
            body = body[: self.max_chars]
        return f"## {self.heading}\n\n{body}"
