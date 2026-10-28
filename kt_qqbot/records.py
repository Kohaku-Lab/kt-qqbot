"""Turn gateway dispatch payloads into plain chat-message records."""

import re
import time
from datetime import datetime
from typing import Any

EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP = "GROUP_MESSAGE_CREATE"
MESSAGE_EVENTS = {EVENT_C2C, EVENT_GROUP_AT, EVENT_GROUP}

MENTION_TAG = re.compile(r"<@!?([A-Za-z0-9_-]+)>")
NATIVE_MENTION = '<qqbot-at-user id="{id}" />'


def native_mentions(text: str, members: dict[str, str]) -> str:
    """Replace ``@<alias>`` of a known member with QQ's native mention tag.

    The longest matching alias wins; unknown names stay plain text.
    """
    if "@" not in text or not members:
        return text
    names = sorted(members, key=len, reverse=True)
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "@":
            name = next((n for n in names if text.startswith(n, i + 1)), None)
            if name is not None:
                out.append(NATIVE_MENTION.format(id=members[name]))
                i += 1 + len(name)
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


_MENTION_ID_FIELDS = ("id", "user_openid", "member_openid", "openid", "union_openid")


def conv_id(kind: str, openid: str) -> str:
    """Return the store key of a conversation: ``c2c:<openid>`` or ``group:<openid>``."""
    return f"{kind}:{openid}"


def split_conv_id(cid: str) -> tuple[str, str]:
    """Return ``(kind, openid)`` of a conversation key."""
    kind, _, openid = cid.partition(":")
    return kind, openid


def _epoch(stamp: Any) -> float:
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if isinstance(stamp, str) and stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def _attachments(data: dict) -> list[dict]:
    items = []
    for att in data.get("attachments") or []:
        if not isinstance(att, dict):
            continue
        url = str(att.get("url") or "")
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url.lstrip('/')}"
        items.append(
            {
                "content_type": str(att.get("content_type") or ""),
                "filename": str(att.get("filename") or ""),
                "url": url,
                "size": att.get("size"),
            }
        )
    return items


def mentions_bot(data: dict, bot_id: str = "") -> bool:
    """Return whether a group message @-mentions the bot.

    The exact full-message-mode marker is not confirmed. Checked shapes:
    a ``mentions`` entry with a truthy ``is_you``, a ``mentions`` entry whose
    id field equals ``bot_id``, and a ``<@id>`` / ``<@!id>`` tag for ``bot_id``.
    """
    for entry in data.get("mentions") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("is_you"):
            return True
        if bot_id and any(
            str(entry.get(f) or "") == bot_id for f in _MENTION_ID_FIELDS
        ):
            return True
    if bot_id:
        return any(
            m == bot_id for m in MENTION_TAG.findall(str(data.get("content") or ""))
        )
    return False


def _strip_bot_tags(content: str, bot_id: str, leading: bool) -> str:
    if leading:
        content = re.sub(r"^\s*<@!?[A-Za-z0-9_-]+>", "", content)
    if bot_id:
        content = re.sub(rf"<@!?{re.escape(bot_id)}>", "", content)
    return content.strip()


def parse_message(event: str, data: dict, bot_id: str = "") -> dict | None:
    """Return a message record for a message dispatch, or None for other events.

    Record keys: ``conv``, ``kind``, ``msg_id``, ``author``, ``name``,
    ``content``, ``ts``, ``attachments``, ``mention``, ``reply_to``, ``self``.
    """
    if event not in MESSAGE_EVENTS:
        return None
    author = data.get("author") or {}
    if event == EVENT_C2C:
        kind = "c2c"
        peer = author.get("user_openid") or author.get("id") or ""
        sender = peer
        mention = True
    else:
        kind = "group"
        peer = data.get("group_openid") or data.get("group_id") or ""
        sender = author.get("member_openid") or author.get("id") or ""
        mention = event == EVENT_GROUP_AT or mentions_bot(data, bot_id)
    msg_id = data.get("id") or ""
    if not peer or not msg_id:
        return None
    reference = data.get("message_reference") or {}
    name = author.get("username") or author.get("nick") or author.get("nickname")
    content = _strip_bot_tags(
        str(data.get("content") or ""), bot_id, leading=event == EVENT_GROUP_AT
    )
    return {
        "conv": conv_id(kind, str(peer)),
        "kind": kind,
        "msg_id": str(msg_id),
        "author": str(sender),
        "name": str(name) if name else "",
        "content": content,
        "ts": _epoch(data.get("timestamp")),
        "attachments": _attachments(data),
        "mention": mention,
        "reply_to": str(reference.get("message_id") or ""),
        "self": False,
    }
