"""On-disk chat log and delivery cursors for one bot, backed by KohakuVault.

Each conversation keeps a bounded, sequence-numbered message log. Each
subscriber (one feed trigger) keeps one cursor per conversation: the last
sequence number it has been shown. Calls are blocking but short SQLite
operations.
"""

import threading
import time
from pathlib import Path
from typing import Any

from kohakuvault import KVault

from kt_qqbot.records import MENTION_TAG

DEFAULT_RETENTION = 2000
DEFAULT_MAX_ALIASES = 1000
HANDLE_PREFIX = {"c2c": "u_", "group": "g_"}


def _seq_key(cid: str, seq: int) -> str:
    return f"log:{cid}:{seq:012d}"


def _cursor_prefix(subscriber: str) -> str:
    return f"cursor:{len(subscriber)}:{subscriber}:"


class ChatStore:
    """Message log, conversation index, aliases and cursors of one bot."""

    def __init__(
        self,
        path: Path | str,
        *,
        retention: int = DEFAULT_RETENTION,
        max_aliases: int = DEFAULT_MAX_ALIASES,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention = max(int(retention), 1)
        self.max_aliases = max(int(max_aliases), 1)
        self._vault = KVault(str(self.path))
        self._vault.enable_auto_pack()
        self._lock = threading.RLock()

    def close(self) -> None:
        """Flush and close the vault."""
        self._vault.close()

    # ── conversations ──────────────────────────────────────────────

    def _ensure_conv(self, cid: str, kind: str) -> dict:
        meta = self._vault.get(f"conv:{cid}")
        if meta is not None:
            return meta
        openid = cid.partition(":")[2]
        handle = self._new_handle(kind, openid, cid)
        meta = {
            "conv": cid,
            "kind": kind,
            "handle": handle,
            "first_seen": time.time(),
            "last_seen": time.time(),
            "head": 0,
            "low": 1,
            "alias_count": 0,
        }
        self._vault[f"conv:{cid}"] = meta
        self._vault[f"handle:{handle}"] = cid
        return meta

    def _new_handle(self, kind: str, openid: str, cid: str) -> str:
        prefix = HANDLE_PREFIX.get(kind, "x_")
        clean = "".join(ch for ch in openid.lower() if ch.isalnum()) or "0"
        size = 6
        while True:
            handle = prefix + clean[:size]
            owner = self._vault.get(f"handle:{handle}")
            if owner is None or owner == cid:
                return handle
            if size >= len(clean):
                handle = f"{handle}{len(self.conversations())}"
                if self._vault.get(f"handle:{handle}") is None:
                    return handle
            size += 4

    def conv_meta(self, cid: str) -> dict | None:
        """Return a conversation's metadata, or None if unseen."""
        return self._vault.get(f"conv:{cid}")

    def conversations(self) -> list[str]:
        """Return every known conversation key."""
        return [
            key.decode()[len("conv:") :] for key in self._vault.keys(prefix="conv:")
        ]

    def handle(self, cid: str) -> str:
        """Return the short handle the model uses for a conversation."""
        meta = self.conv_meta(cid)
        return meta["handle"] if meta else cid

    def resolve_handle(self, handle: str) -> str | None:
        """Return the conversation key behind a handle (or a full key)."""
        found = self._vault.get(f"handle:{handle}")
        if found is not None:
            return found
        return handle if self.conv_meta(handle) is not None else None

    # ── messages ───────────────────────────────────────────────────

    def append(self, record: dict) -> int | None:
        """Store a message and return its sequence number; None for a duplicate id.

        The message, its id index and the new head are written in one batch.
        """
        with self._lock:
            msg_id = record.get("msg_id") or ""
            if msg_id and self._vault.get(f"mid:{msg_id}") is not None:
                return None
            cid = record["conv"]
            meta = self._ensure_conv(cid, record.get("kind", "c2c"))
            seq = int(meta["head"]) + 1
            stored = dict(record, seq=seq)
            if not record.get("self") and record.get("author"):
                stored["alias"] = self.alias(
                    cid, record["author"], record.get("name", "")
                )
            if stored.get("content"):
                stored["content"] = MENTION_TAG.sub(
                    lambda m: "@" + self.alias(cid, m.group(1)), stored["content"]
                )
            meta = dict(self.conv_meta(cid) or meta, head=seq, last_seen=time.time())
            batch: list[tuple[str, object]] = [(_seq_key(cid, seq), stored)]
            if msg_id:
                batch.append((f"mid:{msg_id}", [cid, seq]))
            batch.append((f"conv:{cid}", meta))
            self._vault.put_many(batch)
            self._prune(cid, meta)
            return seq

    def _prune(self, cid: str, meta: dict) -> None:
        low = int(meta["low"])
        while meta["head"] - low + 1 > self.retention:
            old = self._vault.get(_seq_key(cid, low))
            if old is not None:
                self._vault.delete(_seq_key(cid, low))
                if old.get("msg_id"):
                    self._vault.delete(f"mid:{old['msg_id']}")
            low += 1
        if low != meta["low"]:
            self._vault[f"conv:{cid}"] = dict(meta, low=low)

    def head(self, cid: str) -> int:
        """Return the newest sequence number of a conversation (0 if empty)."""
        meta = self.conv_meta(cid)
        return int(meta["head"]) if meta else 0

    def low(self, cid: str) -> int:
        """Return the oldest retained sequence number of a conversation."""
        meta = self.conv_meta(cid)
        return int(meta["low"]) if meta else 1

    def get(self, cid: str, seq: int) -> dict | None:
        """Return one stored message."""
        return self._vault.get(_seq_key(cid, seq))

    def range(self, cid: str, after: int, upto: int | None = None) -> list[dict]:
        """Return messages with ``after < seq <= upto`` that are still retained."""
        meta = self.conv_meta(cid)
        if meta is None:
            return []
        top = meta["head"] if upto is None else min(upto, meta["head"])
        start = max(after + 1, meta["low"])
        if start > top:
            return []
        keys = [_seq_key(cid, s) for s in range(start, top + 1)]
        return [m for m in self._vault.get_many(keys) if m is not None]

    def recent(self, cid: str, limit: int, before: int | None = None) -> list[dict]:
        """Return up to ``limit`` messages older than ``before`` (default: newest)."""
        meta = self.conv_meta(cid)
        if meta is None or limit <= 0:
            return []
        top = meta["head"] if before is None else before - 1
        return self.range(cid, max(top - limit, meta["low"] - 1), top)

    def mark_mention(self, msg_id: str) -> None:
        """Flag a stored message as addressing the bot."""
        found = self.find(msg_id)
        if found is None:
            return
        with self._lock:
            stored = self.get(*found)
            if stored is not None and not stored.get("mention"):
                stored["mention"] = True
                self._vault[_seq_key(*found)] = stored

    def find(self, msg_id: str) -> tuple[str, int] | None:
        """Return ``(conv, seq)`` of a platform message id."""
        found = self._vault.get(f"mid:{msg_id}")
        return (found[0], int(found[1])) if found else None

    # ── aliases ────────────────────────────────────────────────────

    def alias(self, cid: str, author: str, name: str = "") -> str:
        """Return a stable display alias for one sender in one conversation.

        At most ``max_aliases`` per chat are stored; later senders get a
        computed short id that is not stored.
        """
        key = f"alias:{cid}:{author}"
        current = self._vault.get(key)
        if current is None and not self._alias_room(cid):
            return name or "#" + self._clean(author)[:6]
        if name:
            if current != name:
                self._store_alias(cid, key, name, new=current is None)
            return name
        if current:
            return current
        clean = self._clean(author)
        taken = {v for _, v in self._vault.items(prefix=f"alias:{cid}:")}
        size = 4
        alias = "#" + clean[:size]
        while alias in taken and size < len(clean):
            size += 2
            alias = "#" + clean[:size]
        self._store_alias(cid, key, alias, new=True)
        return alias

    def members(self, cid: str) -> dict[str, str]:
        """Map each display alias in ``cid`` to its sender id; shared aliases are left out."""
        prefix = f"alias:{cid}:"
        owners: dict[str, list[str]] = {}
        for key, alias in self._vault.items(prefix=prefix):
            owners.setdefault(alias, []).append(key.decode()[len(prefix) :])
        return {alias: ids[0] for alias, ids in owners.items() if len(ids) == 1}

    @staticmethod
    def _clean(author: str) -> str:
        return "".join(ch for ch in author.lower() if ch.isalnum()) or "0"

    def _alias_room(self, cid: str) -> bool:
        meta = self.conv_meta(cid)
        return meta is None or int(meta.get("alias_count", 0)) < self.max_aliases

    def _store_alias(self, cid: str, key: str, alias: str, *, new: bool) -> None:
        self._vault[key] = alias
        meta = self.conv_meta(cid)
        if new and meta is not None:
            meta["alias_count"] = int(meta.get("alias_count", 0)) + 1
            self._vault[f"conv:{cid}"] = meta

    # ── bot-wide state ─────────────────────────────────────────────

    def get_state(self, name: str, default: Any = None) -> Any:
        """Return one bot-wide value (gateway session, bot identity …)."""
        value = self._vault.get(f"state:{name}")
        return default if value is None else value

    def set_state(self, name: str, value: Any) -> None:
        """Store one bot-wide value."""
        self._vault[f"state:{name}"] = value

    # ── passive-reply slots ────────────────────────────────────────

    def save_slot(self, msg_id: str, slot: dict) -> None:
        """Store one reply slot: ``{conv, used, last_seq, received_at}``."""
        self._vault[f"slot:{msg_id}"] = slot

    def delete_slot(self, msg_id: str) -> None:
        """Remove one reply slot."""
        self._vault.delete(f"slot:{msg_id}")

    def slots(self) -> dict[str, dict]:
        """Return every stored reply slot by msg_id."""
        return {
            key.decode()[len("slot:") :]: value
            for key, value in self._vault.items(prefix="slot:")
        }

    # ── cursors ────────────────────────────────────────────────────

    def cursor(self, subscriber: str, cid: str) -> int | None:
        """Return the last sequence shown to ``subscriber`` (None if never)."""
        value = self._vault.get(_cursor_prefix(subscriber) + cid)
        return int(value) if value is not None else None

    def set_cursor(self, subscriber: str, cid: str, seq: int) -> None:
        """Advance a cursor; it never moves backwards."""
        current = self.cursor(subscriber, cid)
        if current is None or seq > current:
            self._vault[_cursor_prefix(subscriber) + cid] = int(seq)

    def subscribed(self, subscriber: str) -> list[str]:
        """Return the conversations ``subscriber`` has been shown."""
        prefix = _cursor_prefix(subscriber)
        return [key.decode()[len(prefix) :] for key in self._vault.keys(prefix=prefix)]
