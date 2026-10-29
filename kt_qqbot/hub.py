"""Share one QQ gateway connection and chat store per bot across a process.

``acquire`` / ``release`` reference-count a :class:`QQHub` per AppID. The
hub writes every inbound message to its :class:`ChatStore`, wakes the
subscribed feeds, and sends replies through the passive-reply ledger.
"""

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.api import MSG_INPUT_NOTIFY, MSG_MARKDOWN, MSG_MEDIA, MSG_TEXT, QQApi
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import BotInUse, CredentialsMissing, GatewayFatal, QQApiError
from kt_qqbot.filelock import FileLock
from kt_qqbot.gateway import Gateway
from kt_qqbot.ledger import ReplyLedger
from kt_qqbot.outbound import MAX_SINGLE_UPLOAD_BYTES, ChunkedUploader, MediaItem
from kt_qqbot.paths import lock_path, root_dir, vault_path
from kt_qqbot.records import native_mentions, parse_message, split_conv_id
from kt_qqbot.store import ChatStore

logger = get_logger("kt_qqbot.hub")


@dataclass(frozen=True)
class SendResult:
    """Outcome of one ``send``: the stored sequence numbers of what went out."""

    sent: bool
    seqs: tuple[int, ...] = ()
    passive: bool = False
    reason: str = ""

    @property
    def seq(self) -> int | None:
        """Return the sequence number of the last message sent."""
        return self.seqs[-1] if self.seqs else None


CREDS_CHECK_S = 2.0
SESSION_SAVE_S = 1.0
CLOSED_WINDOW = (
    "the reply window has closed (group: someone must speak within 5 min; "
    "private: 60 min). Wait for the next message."
)
# QQ codes for a passive-reply msg_id that expired or ran out of replies.
SLOT_CLOSED_CODES = {40034128, 40034005, 304103}
DUPLICATE_SEQ_CODE = 40054005


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


class HubLock:
    """The per-bot process lock; a second process gets :class:`BotInUse`."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = FileLock(self.path)

    def acquire(self) -> None:
        """Take the lock or raise :class:`BotInUse`."""
        if not self._lock.acquire(blocking=False):
            raise BotInUse(self.path.stem, f"lock {self.path}")

    def release(self) -> None:
        """Drop the lock."""
        self._lock.release()


class QQHub:
    """One bot's connection, store, ledger and subscriber wake-ups."""

    def __init__(
        self,
        creds: Credentials,
        *,
        api: QQApi | None = None,
        store: ChatStore | None = None,
        ledger: ReplyLedger | None = None,
        gateway_factory: Callable[..., Gateway] = Gateway,
        lock: HubLock | None = None,
    ):
        self.creds = creds
        self.lock = lock
        self.app_id = creds.app_id
        self.api = api or QQApi(creds)
        self.store = store or ChatStore(vault_path(creds.app_id))
        self.ledger = ledger or ReplyLedger(store=self.store)
        self._gateway_factory = gateway_factory
        self._session_saved_at = 0.0
        self._session_dirty = False
        self.gateway = self._new_gateway()
        self.chunked_uploader: ChunkedUploader | None = None
        self.unbound = False
        self._task: asyncio.Task | None = None
        self._wakeups: set[asyncio.Event] = set()
        self._new_conv_listeners: list[Callable[[str], Any]] = []
        self._listeners: list[Callable[[str, str], None]] = []
        self._refs = 0
        self._raw_left = 0
        self._raw_path: Path | None = None
        self._creds_checked_at = time.monotonic()
        self._creds_sig = _file_signature(CredentialStore().path)

    def _new_gateway(self) -> Gateway:
        gateway = self._gateway_factory(self.api, self.on_dispatch)
        gateway.on_state = lambda state: self._notify("gateway", "")
        gateway.on_session = self._save_session
        gateway.on_identity = self.remember_identity
        saved = self.store.get_state("gateway") or {}
        if saved.get("session_id") and saved.get("seq") is not None:
            gateway.session_id = saved["session_id"]
            gateway.seq = int(saved["seq"])
        gateway.bot_user = dict(self.store.get_state("bot_user") or {})
        return gateway

    @property
    def bot_identity(self) -> dict:
        """Return what the last READY said about the bot itself (persisted)."""
        return dict(getattr(self.gateway, "bot_user", None) or {})

    def remember_identity(self, user: dict) -> None:
        """Store the bot's own identity from READY so it is known after a restart."""
        self.gateway.bot_user = dict(user)
        self.store.set_state("bot_user", dict(user))
        self._notify("gateway", "")

    def _save_session(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._session_saved_at < SESSION_SAVE_S:
            self._session_dirty = True
            return
        self._session_saved_at = now
        self._session_dirty = False
        gateway = self.gateway
        self.store.set_state(
            "gateway",
            {
                "session_id": getattr(gateway, "session_id", None),
                "seq": getattr(gateway, "seq", None),
            },
        )

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the gateway loop in the background."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"qq-gw-{self.app_id}")

    async def _run(self) -> None:
        try:
            await self.gateway.run()
        except GatewayFatal as exc:
            logger.error("QQ gateway stopped", app_id=self.app_id, error=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.gateway.state = "crashed"
            logger.error(
                "QQ gateway crashed", app_id=self.app_id, error=str(exc), exc_info=True
            )

    async def close(self) -> None:
        """Stop the gateway and close the store and HTTP client."""
        await self._stop_gateway()
        if self._session_dirty or getattr(self.gateway, "session_id", None):
            self._save_session(force=True)
        self.store.close()
        if self.lock is not None:
            self.lock.release()
        await self.api.aclose()

    @property
    def status(self) -> str:
        """Return ``unbound`` or the gateway state (``connecting``, ``ready`` …)."""
        return "unbound" if self.unbound else self.gateway.state

    async def _stop_gateway(self) -> None:
        self.gateway.stop()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def update_credentials(self, creds: Credentials | None) -> None:
        """Switch to new credentials (reconnecting), or stop when ``None`` (unbound)."""
        await self._stop_gateway()
        if creds is None:
            self.unbound = True
            self._notify("gateway", "")
            logger.info("QQ hub unbound", app_id=self.app_id)
            return
        self.unbound = False
        self.creds = creds
        self.api.use_credentials(creds)
        self.gateway = self._new_gateway()
        self.start()
        self._notify("gateway", "")
        logger.info("QQ hub credentials updated", app_id=self.app_id)

    async def refresh_credentials(self, *, force: bool = False) -> None:
        """Follow credential-file changes made by another process (e.g. ``kt-qqbot logout``)."""
        now = time.monotonic()
        if not force and now - self._creds_checked_at < CREDS_CHECK_S:
            return
        self._creds_checked_at = now
        store = CredentialStore()
        signature = _file_signature(store.path)
        if signature == self._creds_sig:
            return
        self._creds_sig = signature
        try:
            creds = store.load(self.app_id)
        except CredentialsMissing:
            creds = None
        if creds is not None and creds == self.creds and not self.unbound:
            return
        await self.update_credentials(creds)

    # ── change listeners ───────────────────────────────────────────

    def add_listener(self, callback: Callable[[str, str], None]) -> None:
        """Call ``callback(kind, conv)`` on ``message``, ``sent`` and ``gateway`` changes."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, str], None]) -> None:
        """Drop a change listener."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, kind: str, cid: str) -> None:
        for callback in list(self._listeners):
            try:
                callback(kind, cid)
            except Exception as exc:
                logger.warning("QQ hub listener failed", error=str(exc))

    # ── inbound ────────────────────────────────────────────────────

    def subscribe(self, wakeup: asyncio.Event) -> None:
        """Set ``wakeup`` whenever a message is stored."""
        self._wakeups.add(wakeup)

    def unsubscribe(self, wakeup: asyncio.Event) -> None:
        """Stop waking ``wakeup``."""
        self._wakeups.discard(wakeup)

    def on_new_conversation(self, callback: Callable[[str], Any]) -> None:
        """Call ``callback(conv)`` when a conversation's first message arrives."""
        self._new_conv_listeners.append(callback)

    def remove_new_conversation(self, callback: Callable[[str], Any]) -> None:
        """Drop a new-conversation callback."""
        if callback in self._new_conv_listeners:
            self._new_conv_listeners.remove(callback)

    @property
    def bot_id(self) -> str:
        """Return the bot's own user id from READY (empty before the first READY)."""
        return str((getattr(self.gateway, "bot_user", None) or {}).get("id") or "")

    def capture_raw(self, count: int) -> None:
        """Write the next ``count`` raw dispatch payloads to ``qq-bot/debug/``."""
        self._raw_left = max(int(count), 0)
        self._raw_path = None

    def _write_raw(self, event: str, data: dict) -> None:
        if self._raw_path is None:
            folder = root_dir() / "debug"
            folder.mkdir(parents=True, exist_ok=True)
            self._raw_path = folder / f"raw-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        fd = os.open(self._raw_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": event, "d": data}, ensure_ascii=False) + "\n")
        self._raw_left -= 1

    async def on_dispatch(self, event: str, data: dict) -> None:
        """Store a gateway message event and wake subscribers."""
        if self._raw_left > 0:
            try:
                self._write_raw(event, data)
            except OSError as exc:
                logger.warning("Raw event capture failed", error=str(exc))
                self._raw_left = 0
        try:
            record = parse_message(event, data, bot_id=self.bot_id)
        except Exception as exc:
            logger.warning("Unparsable QQ event", qq_event=event, error=str(exc))
            return
        if record is None:
            return
        await self.ingest(record)

    async def ingest(self, record: dict) -> int | None:
        """Store one parsed inbound record; return its sequence number."""
        is_new = self.store.conv_meta(record["conv"]) is None
        record.setdefault("recv", time.time())
        if record.get("reply_to") and "to_self" not in record:
            found = self.store.find(record["reply_to"])
            target = self.store.get(*found) if found else None
            record["to_self"] = bool(target and target.get("self"))
            if found and found[0] == record["conv"]:
                record["reply_seq"] = found[1]
        seq = self.store.append(record)
        if seq is None:
            if record.get("mention"):
                self.store.mark_mention(record["msg_id"])
                self._notify("mention", record["conv"])
            return None
        sent_at = min(float(record.get("ts") or record["recv"]), record["recv"])
        self.ledger.note_inbound(record["conv"], record["msg_id"], sent_at)
        for wakeup in list(self._wakeups):
            wakeup.set()
        self._notify("message", record["conv"])
        if is_new:
            for callback in list(self._new_conv_listeners):
                try:
                    result = callback(record["conv"])
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:
                    logger.warning(
                        "New-conversation callback failed",
                        conv=record["conv"],
                        error=str(exc),
                        exc_info=True,
                    )
        return seq

    # ── outbound ───────────────────────────────────────────────────

    async def send(
        self,
        cid: str,
        text: str,
        *,
        by: str = "",
        reply_to_seq: int | None = None,
        media: "list[MediaItem] | tuple[MediaItem, ...]" = (),
        mention_format: str = "plain",
    ) -> SendResult:
        """Send text, then each media item, as separate messages in order.

        Every message takes its own passive-reply slot. The quote, if any,
        goes on the first message. A failure stops the rest. With
        ``mention_format="markdown"``, group text goes out as a markdown
        message and ``@<alias>`` of a known member becomes a native mention
        (the bot needs QQ markdown permission); the stored copy keeps the alias.
        """
        kind, openid = split_conv_id(cid)
        wire_text = text
        markdown = kind == "group" and mention_format == "markdown"
        if markdown:
            wire_text = native_mentions(text, self.store.members(cid))
        await self.refresh_credentials()
        if self.unbound:
            return SendResult(False, reason="the bot is unbound; run kt-qqbot login")
        reference = None
        if reply_to_seq is not None:
            target = self.store.get(cid, reply_to_seq)
            if target is None:
                return SendResult(False, reason=f"m{reply_to_seq} is not in this chat")
            reference = target.get("msg_id") or None
        parts: list[MediaItem | None] = ([None] if text else []) + list(media)
        seqs: list[int] = []
        for item in parts:
            if not self.ledger.usable(cid, kind):
                return SendResult(
                    False, seqs=tuple(seqs), passive=True, reason=CLOSED_WINDOW
                )
            try:
                file_info = None
                if item is not None:
                    file_info = await self._upload(kind, openid, item)
                body = await self._send_on_slot(
                    cid,
                    kind,
                    openid,
                    **self._text_or_media(wire_text, item, markdown),
                    reference_id=reference,
                    media_file_info=file_info,
                )
            except QQApiError as exc:
                reason = exc.message or str(exc)
                logger.info("QQ send failed", conv=cid, error=reason)
                return SendResult(False, seqs=tuple(seqs), passive=True, reason=reason)
            if body is None:
                return SendResult(
                    False, seqs=tuple(seqs), passive=True, reason=CLOSED_WINDOW
                )
            content = text if item is None else f"[{item.media_type}: {item.name}]"
            seq = self._record_self(cid, kind, body, content, reference, by)
            if seq is not None:
                seqs.append(seq)
            reference = None
            self._notify("sent", cid)
        return SendResult(True, seqs=tuple(seqs), passive=True)

    @staticmethod
    def _text_or_media(text: str, item: "MediaItem | None", markdown: bool) -> dict:
        """Message fields for one text or media message."""
        if item is not None:
            return {"content": "", "msg_type": MSG_MEDIA}
        if markdown:
            return {"msg_type": MSG_MARKDOWN, "extra": {"markdown": {"content": text}}}
        return {"content": text, "msg_type": MSG_TEXT}

    async def _send_on_slot(
        self, cid: str, kind: str, openid: str, **message: Any
    ) -> dict | None:
        """Send one message on a passive-reply slot; None when no slot is left.

        A slot QQ reports as expired or used up is closed and the next one is
        tried; a duplicate ``msg_seq`` is retried once with the next seq.
        """
        seq_retried = False
        while True:
            ticket = self.ledger.take(cid, kind)
            if ticket is None:
                return None
            try:
                return await self.api.send_message(
                    kind,
                    openid,
                    msg_id=ticket.msg_id,
                    msg_seq=ticket.msg_seq,
                    **message,
                )
            except QQApiError as exc:
                if exc.code in SLOT_CLOSED_CODES:
                    self.ledger.exhaust(cid, kind, ticket.msg_id)
                    continue
                if exc.code == DUPLICATE_SEQ_CODE and not seq_retried:
                    seq_retried = True
                    continue
                raise

    async def _upload(self, kind: str, openid: str, item: MediaItem) -> str:
        uploader = self.chunked_uploader
        if (
            item.data is not None
            and len(item.data) > MAX_SINGLE_UPLOAD_BYTES
            and uploader is not None
        ):
            return await uploader.upload(kind, openid, item.file_type, item.data)
        try:
            return await self.api.upload_media(
                kind, openid, item.file_type, url=item.url, data=item.data
            )
        except QQApiError:
            if item.url is None or item.fallback is None or not item.fallback.is_file():
                raise
            logger.info("URL upload failed; sending the cached copy", media=item.name)
            return await self.api.upload_media(
                kind, openid, item.file_type, data=item.fallback.read_bytes()
            )

    def _record_self(
        self,
        cid: str,
        kind: str,
        body: dict,
        content: str,
        reference: str | None,
        by: str,
    ) -> int | None:
        return self.store.append(
            {
                "conv": cid,
                "kind": kind,
                "msg_id": str(body.get("id") or ""),
                "author": "",
                "name": "",
                "content": content,
                "ts": time.time(),
                "attachments": [],
                "mention": False,
                "reply_to": reference or "",
                "self": True,
                "by": by,
            }
        )

    async def typing(self, cid: str) -> None:
        """Show the C2C typing indicator; groups have none."""
        kind, openid = split_conv_id(cid)
        if kind != "c2c":
            return
        ticket = self.ledger.take(cid, kind, reserve=1)
        if ticket is None:
            return
        try:
            await self.api.send_message(
                kind,
                openid,
                msg_type=MSG_INPUT_NOTIFY,
                msg_id=ticket.msg_id,
                msg_seq=ticket.msg_seq,
                extra={"input_notify": {"input_type": 1, "input_second": 60}},
            )
        except QQApiError as exc:
            logger.debug("Typing indicator failed", conv=cid, error=str(exc))


# ── process registry ───────────────────────────────────────────────

_HUBS: dict[str, QQHub] = {}


def default_factory(creds: Credentials) -> QQHub:
    """Build and start a real hub; raise :class:`BotInUse` if another process has it."""
    lock = HubLock(lock_path(creds.app_id))
    lock.acquire()
    try:
        hub = QQHub(creds, lock=lock)
    except BaseException:
        lock.release()
        raise
    hub.start()
    return hub


HUB_FACTORY: Callable[[Credentials], QQHub] = default_factory


async def acquire(app_id: str | None = None) -> QQHub:
    """Return the running hub of a bot (the default bot when ``app_id`` is None)."""
    creds = CredentialStore().load(app_id)
    hub = _HUBS.get(creds.app_id)
    if hub is None:
        hub = HUB_FACTORY(creds)
        _HUBS[creds.app_id] = hub
        logger.info("QQ hub opened", app_id=creds.app_id)
    hub._refs += 1
    return hub


async def release(hub: QQHub) -> None:
    """Drop one reference; close the hub when the last user leaves."""
    hub._refs -= 1
    if hub._refs > 0:
        return
    if _HUBS.get(hub.app_id) is hub:
        del _HUBS[hub.app_id]
    await hub.close()
    logger.info("QQ hub closed", app_id=hub.app_id)


async def apply_credentials(app_id: str) -> None:
    """Make a running hub of ``app_id`` use the stored credentials (or unbind it)."""
    hub = _HUBS.get(app_id)
    if hub is None:
        return
    try:
        creds = CredentialStore().load(app_id)
    except CredentialsMissing:
        creds = None
    if creds is not None and creds == hub.creds and not hub.unbound:
        return
    await hub.update_credentials(creds)


def running_hubs() -> dict[str, QQHub]:
    """Return the hubs open in this process."""
    return dict(_HUBS)
