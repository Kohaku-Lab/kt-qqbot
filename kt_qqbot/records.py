"""Turn gateway dispatch payloads into plain chat-message records."""

import re
import time
from datetime import datetime
from typing import Any

EVENT_C2C = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP = "GROUP_MESSAGE_CREATE"
MESSAGE_EVENTS = {EVENT_C2C, EVENT_GROUP_AT, EVENT_GROUP}

_MENTION_TAG = re.compile(r"<@!?[A-Za-z0-9_-]+>")


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


def parse_message(event: str, data: dict) -> dict | None:
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
        mention = event == EVENT_GROUP_AT
    msg_id = data.get("id") or ""
    if not peer or not msg_id:
        return None
    reference = data.get("message_reference") or {}
    name = author.get("username") or author.get("nick") or author.get("nickname")
    content = _MENTION_TAG.sub("", str(data.get("content") or "")).strip()
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
