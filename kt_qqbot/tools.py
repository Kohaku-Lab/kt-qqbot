"""``qq_send`` and ``qq_history``: the creature's explicit voice in QQ chats.

Both tools work through the creature's ``qq_feed`` trigger and only reach
chats that feed has delivered to this creature.
"""

import re
from pathlib import Path
from typing import Any, ClassVar

from kohakuterrarium.modules.tool.base import (
    BaseTool,
    ExecutionMode,
    ToolConfig,
    ToolContext,
    ToolResult,
)
from kohakuterrarium.modules.trigger.callable import CallableTriggerTool
from kohakuterrarium.utils.logging import get_logger

from kt_qqbot import access
from kt_qqbot.errors import MediaError
from kt_qqbot.outbound import MAX_SINGLE_UPLOAD_BYTES as MAX_UPLOAD
from kt_qqbot.outbound import MEDIA_TYPES, MediaItem, parse_media_arg, prepare
from kt_qqbot.trigger import QQFeedTrigger, feeds_of

logger = get_logger("kt_qqbot.tools")

MAX_MESSAGE_CHARS = 2000
HISTORY_MAX = 100
_TAG = re.compile(r"^m?(\d+)$")

SEND_DOC = """# qq_send

Send one chat message to a QQ chat (private or group) that your feed delivered.

## Arguments

| Arg | Type | Description |
|-----|------|-------------|
| message | string | The text of one chat message. |
| to | string | Chat handle such as `g_ab12cd` or `u_ab12cd`. Optional when your last delivery came from one chat. |
| reply_to | string | Message tag such as `m41` to quote. Optional. |
| media | string or list | A local path (relative to your working folder) or an https URL; a list is sent in order. Optional. |
| media_type | string | `image`, `video`, `voice` or `file`. Optional; inferred from the file name. |

## Behavior

- The result is `Sent as m57`, or `NOT SENT: <reason>`.
- If chat arrived in that chat since your last delivery, the first call returns
  that chat and does not send. Read it, then call again (same or revised text)
  or stop.
- You can only send to chats your feed delivered to you.
- Text is sent first, then each media item as its own message. Files you made
  (for example generated images) can be sent by path.
- Voice must be SILK; other audio is converted when the voice codec is
  installed. Local files over 5 MB cannot be sent yet.
- Group chats do not take `file` messages.
- A reply rides on a recent incoming message. When the chat has been silent too
  long, QQ may refuse the send; the result says so.
"""

HISTORY_DOC = """# qq_history

Read older messages of a QQ chat your feed delivered.

## Arguments

| Arg | Type | Description |
|-----|------|-------------|
| chat | string | Chat handle. Optional when your last delivery came from one chat. |
| before | string | Message tag; returns messages older than it. Default: the newest messages. |
| limit | integer | How many messages (1-100, default 20). |
"""


def _tags(seqs: tuple[int, ...]) -> str:
    return ", ".join(f"m{s}" for s in seqs)


def _seq(tag: Any) -> int | None:
    if tag is None or tag == "":
        return None
    match = _TAG.match(str(tag).strip())
    return int(match.group(1)) if match else None


def resolve_chat(
    context: ToolContext | None, handle: str | None
) -> tuple[QQFeedTrigger, str] | str:
    """Return ``(feed, conv)`` for a chat handle, or an error message."""
    feeds = feeds_of(getattr(context, "agent", None))
    if not feeds:
        return "this creature has no running qq_feed trigger"
    if not handle:
        recent = {(f, c) for f in feeds for c in f.last_delivered}
        if len(recent) == 1:
            return next(iter(recent))
        handles = sorted(f.hub.store.handle(c) for f, c in recent)
        if not handles:
            return "no chat delivered yet; say which chat with `to`"
        return f"say which chat with `to`; recent chats: {', '.join(handles)}"
    for feed in feeds:
        cid = feed.hub.store.resolve_handle(handle.strip())
        if cid is not None and feed.known(cid):
            return feed, cid
    return f"unknown chat {handle!r}; you can only use chats your feed delivered"


class QQSendTool(BaseTool):
    """Send a message to a QQ chat through the shared bot hub."""

    needs_context = True
    is_concurrency_safe = False
    path_arguments: ClassVar[dict[str, str]] = {"media": "read"}

    def __init__(self, config: ToolConfig | None = None, **options: Any):
        super().__init__(config)
        self.max_chars = int(options.get("max_chars", MAX_MESSAGE_CHARS))
        self.allow_group_files = bool(options.get("allow_group_files", False))
        self.media_roots = [
            Path(root).expanduser().resolve()
            for root in options.get("media_roots") or []
        ]

    @property
    def tool_name(self) -> str:
        return "qq_send"

    @property
    def description(self) -> str:
        return (
            "Send a chat message and/or media (image, video, voice, file) to a QQ "
            "chat your feed delivered. Not for answering the operator console."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        """Describe the arguments for native function calling."""
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "One chat message."},
                "to": {
                    "type": "string",
                    "description": "Chat handle; optional if only one chat is active.",
                },
                "reply_to": {
                    "type": "string",
                    "description": "Message tag to quote, e.g. m41.",
                },
                "media": {
                    "anyOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Local path or https URL, or a list sent in order.",
                },
                "media_type": {
                    "type": "string",
                    "enum": sorted(MEDIA_TYPES),
                    "description": "Override the type inferred from the file name.",
                },
            },
        }

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return SEND_DOC

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Send, or return chat that arrived since the last delivery."""
        if not access.allowed(context):
            return ToolResult(error=access.ADMIN_ONLY)
        message = str(args.get("message") or "").strip()
        try:
            specs = parse_media_arg(args.get("media"))
        except MediaError as exc:
            return ToolResult(error=f"NOT SENT: {exc}")
        if not message and not specs:
            return ToolResult(error="NOT SENT: empty message")
        if len(message) > self.max_chars:
            return ToolResult(
                error=f"NOT SENT: message longer than {self.max_chars} characters; "
                "split it into shorter chat messages"
            )
        resolved = resolve_chat(context, args.get("to"))
        if isinstance(resolved, str):
            return ToolResult(error=f"NOT SENT: {resolved}")
        feed, cid = resolved

        if feed.hold and cid not in feed.hold_bypass:
            unseen = [m for m in feed.pending(cid) if not m.get("self")]
            if unseen:
                feed.take(cid)
                feed.hold_bypass.add(cid)
                return ToolResult(
                    output=(
                        "NOT SENT: new chat arrived while you were thinking.\n"
                        f"{feed.render_held(cid, unseen)}\n"
                        "Call qq_send again to send (as is or revised), "
                        "or stop if it no longer fits."
                    ),
                    exit_code=0,
                )
        feed.hold_bypass.discard(cid)

        reply_seq = _seq(args.get("reply_to"))
        if args.get("reply_to") and reply_seq is None:
            return ToolResult(error=f"NOT SENT: bad reply_to {args.get('reply_to')!r}")
        try:
            media = await self._prepare_media(
                specs, args.get("media_type") or None, cid, feed, context
            )
        except MediaError as exc:
            return ToolResult(error=f"NOT SENT: {exc}")

        result = await feed.hub.send(
            cid,
            message,
            by=feed.subscriber or "",
            reply_to_seq=reply_seq,
            media=media,
        )
        if result.sent:
            return ToolResult(output=f"Sent as {_tags(result.seqs)}", exit_code=0)
        if result.seqs:
            return ToolResult(
                output=(
                    f"PARTLY SENT as {_tags(result.seqs)}; "
                    f"NOT SENT the rest: {result.reason}"
                ),
                exit_code=1,
            )
        return ToolResult(output=f"NOT SENT: {result.reason}", exit_code=1)

    async def _prepare_media(
        self,
        specs: list[str],
        media_type: str | None,
        cid: str,
        feed: QQFeedTrigger,
        context: ToolContext | None,
    ) -> list[MediaItem]:
        base = context.working_dir if context is not None else Path.cwd()
        roots = [Path(base).resolve(), *self.media_roots]

        def resolve(spec: str) -> Path:
            path = (Path(base) / Path(spec).expanduser()).resolve()
            if not any(path.is_relative_to(root) for root in roots):
                raise MediaError(
                    f"media path {spec!r} is outside the working folder; "
                    "move the file there (or add its folder to media_roots)"
                )
            return path

        limit = None if feed.hub.chunked_uploader is not None else MAX_UPLOAD
        items = [
            await prepare(spec, media_type, resolve, size_limit=limit) for spec in specs
        ]
        if cid.startswith("group:") and not self.allow_group_files:
            if any(item.media_type == "file" for item in items):
                raise MediaError(
                    "QQ group chats do not take file messages; send it in a "
                    "private chat, or as an image or video"
                )
        return items


class QQHistoryTool(BaseTool):
    """Read older messages of a delivered QQ chat from the on-disk log."""

    needs_context = True

    def __init__(self, config: ToolConfig | None = None, **options: Any):
        super().__init__(config)

    @property
    def tool_name(self) -> str:
        return "qq_history"

    @property
    def description(self) -> str:
        return (
            "Read older messages of a QQ chat your feed delivered. "
            "Not for new messages, which arrive in your feed."
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.DIRECT

    def get_parameters_schema(self) -> dict:
        """Describe the arguments for native function calling."""
        return {
            "type": "object",
            "properties": {
                "chat": {"type": "string", "description": "Chat handle."},
                "before": {
                    "type": "string",
                    "description": "Message tag; read messages older than it.",
                },
                "limit": {"type": "integer", "description": "1-100, default 20."},
            },
        }

    def get_full_documentation(self, tool_format: str = "native") -> str:
        return HISTORY_DOC

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Return rendered older messages."""
        if not access.allowed(context):
            return ToolResult(error=access.ADMIN_ONLY)
        resolved = resolve_chat(context, args.get("chat"))
        if isinstance(resolved, str):
            return ToolResult(error=resolved)
        feed, cid = resolved
        try:
            limit = max(1, min(int(args.get("limit") or 20), HISTORY_MAX))
        except (TypeError, ValueError):
            return ToolResult(error=f"bad limit {args.get('limit')!r}")
        before = _seq(args.get("before"))
        if args.get("before") and before is None:
            return ToolResult(error=f"bad before {args.get('before')!r}")
        store = feed.hub.store
        upto = store.cursor(feed.subscriber, cid) or 0
        if before is None or before > upto + 1:
            before = upto + 1
        messages = store.recent(cid, limit, before=before)
        if not messages:
            return ToolResult(output="(no older messages kept)", exit_code=0)
        return ToolResult(
            output=feed.render_held(cid, messages, name="qq_history"), exit_code=0
        )


class QQWatchTool(CallableTriggerTool):
    """``qq_watch``: let the creature install a ``qq_feed`` trigger at runtime."""

    def __init__(self, config: ToolConfig | None = None, **options: Any):
        super().__init__(QQFeedTrigger)

    async def _execute(
        self, args: dict[str, Any], context: ToolContext | None = None, **kwargs: Any
    ) -> ToolResult:
        """Install a feed unless the creature may not use the bot."""
        if not access.allowed(context):
            return ToolResult(error=access.ADMIN_ONLY)
        return await super()._execute(args, context=context)
