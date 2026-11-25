"""Read quotes and shared cards out of a QQ message payload.

The official docs omit these fields; the shapes are inferred from other
projects' decoders (LuckyLilliaBot ``qq-official/{types,decode}.ts``).
``message_scene.ext`` holds ``msg_idx=REFIDX_…`` (this message) and
``ref_msg_idx=REFIDX_…`` (the quoted one). With ``message_type == 103`` or a
``ref_msg_idx``, ``msg_elements[0]`` is the quoted message: ``content`` as
``"=== 消息 1 ===\\n[消息内容] text\\n[消息类型] …"``, plus ``attachments``,
``author`` and ``msg_idx``. A shared card carries ``ark = {template_id, kv:
[{key, value, obj: [{obj_kv: [{key, value}]}]}]}`` and may carry images in
``msg_elements[*].attachments``. Unknown shapes give empty results.
"""

import re
from typing import Any

QUOTE_MESSAGE_TYPE = 103
QUOTE_TEXT_CHARS = 500
CARD_TEXT_CHARS = 120

_BODY_START = re.compile(r"^\s*\[消息内容\]\s*")
_BODY_END = re.compile(r"^\s*(?:\[消息类型\]|\[关联消息\]|---|===)")
_IMAGE_KEY = re.compile(r"PREVIEW|COVER|IMAGE|IMG|PIC|THUMB", re.IGNORECASE)
_TITLE_KEY = re.compile(r"TITLE|PROMPT", re.IGNORECASE)
_DESC_KEY = re.compile(r"DESC|SUMMARY|SUBTITLE", re.IGNORECASE)
_URL_LIKE = re.compile(r"^(?:https?:)?//[^\s/]+\.[^\s]+$", re.IGNORECASE)
_AUTHOR_FIELDS = ("member_openid", "user_openid", "id", "union_openid")
_NAME_FIELDS = ("username", "nick", "nickname")


def scene_value(data: dict, key: str) -> str:
    """Return ``<key>=…`` from ``message_scene.ext``, or ``""``."""
    scene = data.get("message_scene")
    ext = scene.get("ext") if isinstance(scene, dict) else None
    if not isinstance(ext, list):
        return ""
    prefix = key + "="
    for item in ext:
        if isinstance(item, str) and item.startswith(prefix):
            return item[len(prefix) :]
    return ""


def _elements(data: dict) -> list[dict]:
    items = data.get("msg_elements")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def https_url(url: Any) -> str:
    """Return ``url`` as an absolute https URL, or ``""`` when it is not a URL."""
    if not isinstance(url, str) or not url.strip():
        return ""
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith(("http://", "https://")):
        return url
    if re.match(r"^[a-z][a-z\d+.-]*:", url, re.IGNORECASE):
        return ""
    return "https://" + url.lstrip("/")


def attachment(att: Any) -> dict | None:
    """Return one normalized attachment record, or None for a non-dict."""
    if not isinstance(att, dict):
        return None
    return {
        "content_type": str(att.get("content_type") or ""),
        "filename": str(att.get("filename") or ""),
        "url": https_url(att.get("url")),
        "size": att.get("size"),
    }


def attachments(items: Any) -> list[dict]:
    """Return the normalized attachments of a payload's ``attachments`` list."""
    if not isinstance(items, list):
        return []
    return [a for a in (attachment(item) for item in items) if a is not None]


def quoted_text(content: str) -> str:
    """Return the text after ``[消息内容]`` up to the next platform marker line.

    Content without the marker is returned as is.
    """
    lines = content.replace("\r\n", "\n").split("\n")
    start = next((i for i, line in enumerate(lines) if _BODY_START.match(line)), -1)
    if start < 0:
        return content.strip()
    body = [_BODY_START.sub("", lines[start])]
    for line in lines[start + 1 :]:
        if _BODY_END.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def is_quote(data: dict) -> bool:
    """Return whether the payload quotes another message the new way."""
    return bool(
        scene_value(data, "ref_msg_idx")
        or data.get("message_type") == QUOTE_MESSAGE_TYPE
    )


def read_quote(data: dict) -> dict | None:
    """Return ``{text, attachments, author, name, idx}`` of the quoted message.

    None when the payload quotes nothing. A quote whose element is missing
    still returns the referenced ``idx`` with empty text.
    """
    if not is_quote(data):
        return None
    elements = _elements(data)
    element = elements[0] if elements else {}
    author = element.get("author")
    author = author if isinstance(author, dict) else {}
    content = element.get("content")
    text = quoted_text(content) if isinstance(content, str) else ""
    return {
        "text": text[:QUOTE_TEXT_CHARS],
        "attachments": attachments(element.get("attachments")),
        "author": next((str(author[f]) for f in _AUTHOR_FIELDS if author.get(f)), ""),
        "name": next((str(author[f]) for f in _NAME_FIELDS if author.get(f)), ""),
        "idx": scene_value(data, "ref_msg_idx") or str(element.get("msg_idx") or ""),
    }


def _ark_pairs(ark: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    entries = ark.get("kv")
    if not isinstance(entries, list):
        return pairs
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        if isinstance(entry.get("value"), str):
            pairs.append((key, entry["value"]))
        objects = entry.get("obj")
        for obj in objects if isinstance(objects, list) else []:
            inner = obj.get("obj_kv") if isinstance(obj, dict) else None
            for kv in inner if isinstance(inner, list) else []:
                if isinstance(kv, dict) and isinstance(kv.get("value"), str):
                    pairs.append((str(kv.get("key") or ""), kv["value"]))
    return pairs


def _image_type(url: str) -> str:
    lower = url.lower().split("?", 1)[0]
    for suffix, mime in ((".png", "image/png"), (".gif", "image/gif")):
        if lower.endswith(suffix):
            return mime
    if lower.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def read_card(data: dict, *, quoting: bool = False) -> tuple[str | None, list[dict]]:
    """Return ``(card label, image attachments)`` of a shared card.

    Images come from ark ``kv`` entries whose key names a preview / cover /
    image, and from ``msg_elements[*].attachments`` (skipped when
    ``quoting``, since the first element is then the quoted message). The
    label is ``"title - desc"`` (possibly ``""``) when an ark is present,
    else None.
    """
    images: list[dict] = []
    seen: set[str] = set()

    def add(att: dict | None) -> None:
        if att is None or not att["url"] or att["url"] in seen:
            return
        seen.add(att["url"])
        images.append(att)

    if not quoting:
        for element in _elements(data):
            for att in attachments(element.get("attachments")):
                add(att)
    ark = data.get("ark")
    if not isinstance(ark, dict):
        return None, images
    title = desc = ""
    for key, value in _ark_pairs(ark):
        if _IMAGE_KEY.search(key):
            url = https_url(value) if _URL_LIKE.match(value.strip()) else ""
            if url:
                add(
                    {
                        "content_type": _image_type(url),
                        "filename": "",
                        "url": url,
                        "size": None,
                    }
                )
        elif _TITLE_KEY.search(key) and not title:
            title = value.strip()
        elif _DESC_KEY.search(key) and not desc:
            desc = value.strip()
    label = " - ".join(part for part in (title, desc) if part)
    return label[:CARD_TEXT_CHARS], images
