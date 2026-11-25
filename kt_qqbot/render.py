"""Render stored QQ messages as the chat-log text a creature reads."""

import re
from datetime import datetime

from kohakuterrarium.llm.message import ContentPart, ImagePart, TextPart

QUOTE_PREVIEW_CHARS = 40
NAME_CHARS = 32
CARD_CHARS = 80
CONTINUATION = "\n    "
CHAT_TYPES = {"c2c": "private", "group": "group"}

_FEED_TAG = re.compile(r"<(\s*/?\s*)(qq_)", re.IGNORECASE)
_SPACES = re.compile(r"\s+")


def neutralize(text: str) -> str:
    """Turn every ``<qq_…`` / ``</qq_…`` sequence into ``‹qq_…`` so it opens no block."""
    return _FEED_TAG.sub(lambda m: "‹" + m.group(1).strip() + m.group(2), str(text))


def safe_text(text: str) -> str:
    """Message body: tags neutralized, continuation lines indented."""
    body = neutralize(text).replace("\r\n", "\n").replace("\r", "\n")
    return body.replace("\n", CONTINUATION)


def safe_name(text: str, limit: int = NAME_CHARS) -> str:
    """One-line label without brackets, so it cannot imitate ``[you]`` or markers."""
    label = _SPACES.sub(" ", neutralize(text).replace("[", "").replace("]", "")).strip()
    return _clip(label, limit) if label else "?"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _clock(ts: float | None) -> str:
    if not ts:
        return "--:--"
    return datetime.fromtimestamp(float(ts)).strftime("%H:%M")


def media_kind(content_type: str) -> str:
    """Return image / voice / video / file for an attachment content type."""
    kind = str(content_type or "")
    if kind.startswith("image/"):
        return "image"
    if kind.startswith("voice") or kind.startswith("audio/"):
        return "voice"
    if kind.startswith("video/"):
        return "video"
    return "file"


def _media(message: dict) -> str:
    marks = []
    seq = message.get("seq")
    for index, att in enumerate(message.get("attachments") or [], start=1):
        handle = f"m{seq}.{index}"
        kind = media_kind(att.get("content_type", ""))
        if kind == "file":
            marks.append(f"[file:{safe_name(att.get('filename') or 'file')} {handle}]")
        else:
            marks.append(f"[{kind} {handle}]")
    return (" " + " ".join(marks)) if marks else ""


def _preview(text: str) -> str:
    return _clip(_SPACES.sub(" ", neutralize(text)).strip(), QUOTE_PREVIEW_CHARS)


def _quote_marks(items: list) -> str:
    kinds = [
        media_kind(a.get("content_type", "")) for a in items if isinstance(a, dict)
    ]
    return " ".join(f"[{kind}]" for kind in kinds)


def _platform_quote(quote: dict) -> str:
    """Render a quote the platform carried inline (the quoted message is not stored)."""
    who = "you" if quote.get("self") else quote.get("alias") or quote.get("name")
    parts = ["↩"]
    if who:
        parts.append(who if who == "you" else safe_name(who))
    preview = _preview(str(quote.get("text") or ""))
    if preview:
        parts.append(f'"{preview}"')
    marks = _quote_marks(quote.get("attachments") or [])
    if marks:
        parts.append(marks)
    if len(parts) == 1:
        parts.append("(older message)")
    return " " + parts[0] + " ".join(parts[1:])


def render_line(message: dict, quoted: dict | None = None) -> str:
    """Render one message as ``[HH:MM] m<seq> <sender>[ markers]: text``.

    ``quoted`` is the stored message it quotes; without one, a quote the
    platform carried in the message itself is shown instead.
    """
    tag = f"m{message.get('seq')}"
    if message.get("self"):
        sender = "[you]"
    else:
        sender = safe_name(message.get("alias") or message.get("name") or "?")
        if message.get("kind") == "group" and message.get("mention"):
            sender += " [@you]"
    quote = ""
    if quoted is not None:
        who = "you" if quoted.get("self") else safe_name(quoted.get("alias") or "?")
        preview = _preview(quoted.get("content", ""))
        quote = f' ↩m{quoted.get("seq")} {who} "{preview}"'
    elif isinstance(message.get("quote"), dict):
        quote = _platform_quote(message["quote"])
    elif message.get("reply_to"):
        quote = " ↩(older message)"
    text = safe_text(message.get("content", ""))
    card = message.get("card")
    if card is not None:
        label = safe_name(card, CARD_CHARS) if card else ""
        text = f"{text} [card: {label}]" if label else f"{text} [card]"
        text = text.lstrip()
    return (
        f"[{_clock(message.get('ts'))}] {tag} {sender}{quote}: {text}{_media(message)}"
    )


def render_block(name: str, attrs: dict[str, str], lines: list[str]) -> str:
    """Wrap lines in ``<name attr="…">…</name>``."""
    rendered = " ".join(f'{k}="{v}"' for k, v in attrs.items() if v)
    head = f"<{name} {rendered}>" if rendered else f"<{name}>"
    return "\n".join([head, *lines, f"</{name}>"])


def chat_label(name: str) -> str:
    """Return a chat name safe inside a block attribute, or ``""``."""
    if not str(name or "").strip():
        return ""
    return safe_name(name).replace('"', "'").replace("<", "‹").replace(">", "›")


def chat_attrs(handle: str, kind: str, name: str = "") -> dict[str, str]:
    """Return the attributes that identify a chat block (``name`` when known)."""
    return {
        "chat": handle,
        "name": chat_label(name),
        "type": CHAT_TYPES.get(kind, kind),
    }


def chat_title(handle: str, name: str = "") -> str:
    """Return ``handle (name)`` for lists of chats, or the handle alone."""
    label = chat_label(name)
    return f"{handle} ({label})" if label else handle


def combine(text: str, images: list[ImagePart]) -> "str | list[ContentPart]":
    """Return plain text, or text plus images as multimodal content."""
    if not images:
        return text
    return [TextPart(text=text), *images]
