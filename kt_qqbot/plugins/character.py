"""``qq_character`` plugin: add the user's character files to the system prompt.

Files come from ``<config>/qq-bot/``: ``character.md`` first, then every
``*.md`` under ``character/`` in name order. Missing files add nothing.
A persona image (``character.png`` / ``.jpg`` / ``.jpeg`` / ``.webp``) is
pinned as a message right after the system prompt.
"""

from pathlib import Path
from typing import Any

from kohakuterrarium.modules.plugin.base import BasePlugin, PluginContext
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.paths import root_dir
from kt_qqbot.persona import find_persona, persona_content, prepare_persona

logger = get_logger("kt_qqbot.character")

MAX_CHARS = 20000
PIN_KEY = "qq_persona"


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
        persona: bool = True,
    ):
        super().__init__()
        self.directory = Path(directory).expanduser() if directory else None
        self.heading = heading
        self.max_chars = int(max_chars)
        self.persona = bool(persona)
        self._logged: tuple | None = None
        self._ctx: Any = None
        self._persona_sig: tuple | None = None
        self._ignored: tuple[str, ...] = ()
        self._pinned = False

    async def on_load(self, context: PluginContext) -> None:
        self._ctx = context

    async def on_agent_start(self) -> None:
        self.sync_persona()

    async def on_event(self, event: Any) -> None:
        self.sync_persona()

    def sync_persona(self) -> None:
        """Pin, re-pin or unpin the persona image when its file changed."""
        if not self.persona:
            return
        base = self.directory or root_dir()
        chosen, ignored = find_persona(base)
        names = tuple(p.name for p in ignored)
        if names and names != self._ignored:
            logger.warning(
                "Several persona images; using the first",
                file=chosen.name if chosen else "",
                ignored=list(names),
            )
        self._ignored = names
        signature = None
        if chosen is not None:
            stat = chosen.stat()
            signature = (chosen.name, stat.st_mtime_ns, stat.st_size)
        if signature == self._persona_sig:
            return
        self._persona_sig = signature
        if chosen is None:
            self._unpin()
            return
        try:
            prepared = prepare_persona(chosen, root_dir() / "cache")
        except (OSError, ValueError) as exc:
            logger.warning("Persona image unreadable", file=chosen.name, error=str(exc))
            self._unpin()
            return
        logger.info(
            "Persona image loaded",
            file=chosen.name,
            width=prepared.width,
            height=prepared.height,
            bytes=prepared.bytes,
        )
        if self._ctx is not None:
            self._pinned = bool(
                self._ctx.pin_message(PIN_KEY, "user", persona_content(prepared))
            )

    def _unpin(self) -> None:
        if self._pinned and self._ctx is not None:
            self._ctx.unpin_message(PIN_KEY)
        self._pinned = False

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

    def _log_load(self, files: list[Path], chars: int) -> None:
        """Log file names and size when the loaded set or its signature changes."""
        signature = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
        key = tuple(signature)
        if key == self._logged:
            return
        self._logged = key
        logger.info(
            "Character loaded",
            files=[name for name, _, _ in signature],
            chars=chars,
        )

    def get_prompt_content(self, context: PluginContext) -> str | None:
        """Return the character section, or None when no file has content."""
        files = self.files()
        parts = []
        for path in files:
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
        self._log_load(files, len(body))
        if len(body) > self.max_chars:
            logger.warning(
                "Character files truncated", chars=len(body), limit=self.max_chars
            )
            body = body[: self.max_chars]
        return f"## {self.heading}\n\n{body}"
