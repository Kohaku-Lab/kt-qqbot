"""Share one QQ gateway connection and chat store per bot across a process.

``acquire`` / ``release`` reference-count a :class:`QQHub` per AppID. The
hub writes every inbound message to its :class:`ChatStore`, wakes the
subscribed feeds, and sends replies through the passive-reply ledger.
"""

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.api import MSG_INPUT_NOTIFY, MSG_MEDIA, MSG_TEXT, QQApi
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import BotInUse, CredentialsMissing, GatewayFatal, QQApiError
from kt_qqbot.filelock import FileLock
from kt_qqbot.gateway import Gateway
from kt_qqbot.ledger import ReplyLedger
from kt_qqbot.outbound import MAX_SINGLE_UPLOAD_BYTES, ChunkedUploader, MediaItem
from kt_qqbot.paths import lock_path, vault_path
from kt_qqbot.records import parse_message, split_conv_id
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
        self.gateway = self._new_gateway()
        self.chunked_uploader: ChunkedUploader | None = None
        self.unbound = False
        self._task: asyncio.Task | None = None
        self._wakeups: set[asyncio.Event] = set()
        self._new_conv_listeners: list[Callable[[str], Any]] = []
        self._listeners: list[Callable[[str, str], None]] = []
        self._refs = 0
        self._creds_checked_at = time.monotonic()
        self._creds_sig = _file_signature(CredentialStore().path)

    def _new_gateway(self) -> Gateway:
        gateway = self._gateway_factory(self.api, self.on_dispatch)
        gateway.on_state = lambda state: self._notify("gateway", "")
        return gateway

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

    async def on_dispatch(self, event: str, data: dict) -> None:
        """Store a gateway message event and wake subscribers."""
        try:
            record = parse_message(event, data)
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
    ) -> SendResult:
        """Send text, then each media item, as separate messages in order.

        Every message takes its own passive-reply slot. The quote, if any,
        goes on the first message. A failure stops the rest.
        """
        kind, openid = split_conv_id(cid)
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
        passive = False
        for item in parts:
            ticket = None
            try:
                file_info = None
                if item is not None:
                    file_info = await self._upload(kind, openid, item)
                ticket = self.ledger.take(cid, kind)
                body = await self.api.send_message(
                    kind,
                    openid,
                    content=text if item is None else "",
                    msg_type=MSG_TEXT if item is None else MSG_MEDIA,
                    msg_id=ticket.msg_id if ticket else None,
                    msg_seq=ticket.msg_seq if ticket else None,
                    reference_id=reference,
                    media_file_info=file_info,
                )
            except QQApiError as exc:
                reason = exc.message or str(exc)
                if ticket is None and (item is None or file_info is not None):
                    reason = (
                        "no recent message to reply to (the passive-reply window "
                        f"has passed) and QQ refused an active message: {reason}"
                    )
                logger.info("QQ send failed", conv=cid, error=reason)
                return SendResult(
                    False, seqs=tuple(seqs), passive=passive, reason=reason
                )
            passive = passive or ticket is not None
            content = text if item is None else f"[{item.media_type}: {item.name}]"
            seq = self._record_self(cid, kind, body, content, reference, by)
            if seq is not None:
                seqs.append(seq)
            reference = None
            self._notify("sent", cid)
        return SendResult(True, seqs=tuple(seqs), passive=passive)

    async def _upload(self, kind: str, openid: str, item: MediaItem) -> str:
        uploader = self.chunked_uploader
        if (
            item.data is not None
            and len(item.data) > MAX_SINGLE_UPLOAD_BYTES
            and uploader is not None
        ):
            return await uploader.upload(kind, openid, item.file_type, item.data)
        return await self.api.upload_media(
            kind, openid, item.file_type, url=item.url, data=item.data
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
        ticket = self.ledger.take(cid, kind)
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
