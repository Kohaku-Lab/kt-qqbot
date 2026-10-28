"""QQ bot gateway WebSocket: identify / resume, heartbeat, reconnect."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from kohakuterrarium.utils.logging import get_logger

from kt_qqbot.api import QQApi
from kt_qqbot.errors import GatewayFatal, QQBotError

logger = get_logger("kt_qqbot.gateway")

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENT_GROUP_AND_C2C = 1 << 25
DEFAULT_INTENTS = INTENT_GROUP_AND_C2C

BACKOFF_S = (1, 2, 5, 10, 30, 60)
RATE_LIMIT_WAIT_S = 60
CLOSE_INVALID_TOKEN = 4004
CLOSE_RATE_LIMITED = 4008
FATAL_CLOSE_CODES = {4914, 4915}
# Close codes after which the old session cannot be resumed.
SESSION_RESET_CODES = {4004, 4006, 4007, 4009}

DispatchHandler = Callable[[str, dict], Awaitable[None] | None]


class Gateway:
    """Keep one gateway session alive and hand dispatch events to a callback."""

    def __init__(
        self,
        api: QQApi,
        on_dispatch: DispatchHandler,
        *,
        intents: int = DEFAULT_INTENTS,
        connect: Callable[..., Any] = websockets.connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff: tuple[float, ...] = BACKOFF_S,
        on_ready: Callable[[], Awaitable[None] | None] | None = None,
    ):
        self.api = api
        self.on_dispatch = on_dispatch
        self.intents = intents
        self._connect = connect
        self._sleep = sleep
        self._backoff = backoff
        self._on_ready = on_ready
        self.session_id: str | None = None
        self.seq: int | None = None
        self.ready = asyncio.Event()
        self.on_state: Callable[[str], None] | None = None
        self._state = "idle"
        self.last_error = ""
        self._stopping = False

    @property
    def state(self) -> str:
        """Connection state: idle, connecting, ready, reconnecting, fatal, stopped."""
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        changed = value != self._state
        self._state = value
        if changed and self.on_state is not None:
            self.on_state(value)

    def stop(self) -> None:
        """Ask the run loop to end after the current connection."""
        self._stopping = True

    async def run(self) -> None:
        """Connect and reconnect until stopped or a fatal close."""
        failures = 0
        while not self._stopping:
            self.state = "connecting"
            delay: float | None = None
            try:
                url = await self.api.gateway_url()
                async with self._connect(url) as ws:
                    await self._session(ws)
                failures = 0
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd is not None else 1006
                reason = exc.rcvd.reason if exc.rcvd is not None else ""
                self.last_error = f"closed {code} {reason}".strip()
                if code in FATAL_CLOSE_CODES:
                    self.state = "fatal"
                    raise GatewayFatal(code, reason) from exc
                if code in SESSION_RESET_CODES:
                    self.session_id, self.seq = None, None
                if code == CLOSE_INVALID_TOKEN:
                    self.api.tokens.invalidate()
                if code == CLOSE_RATE_LIMITED:
                    delay = RATE_LIMIT_WAIT_S
                if self.ready.is_set():
                    failures = 0
            except GatewayFatal:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                expected = isinstance(exc, (OSError, QQBotError, asyncio.TimeoutError))
                logger.warning(
                    "Gateway session failed; reconnecting",
                    error=self.last_error,
                    exc_info=not expected,
                )
            finally:
                self.ready.clear()
                if self.state == "ready":
                    self.state = "reconnecting"
            if self._stopping:
                break
            if delay is None:
                delay = self._backoff[min(failures, len(self._backoff) - 1)]
            failures += 1
            self.state = "reconnecting"
            await self._sleep(delay)
        self.state = "stopped"

    async def _session(self, ws: Any) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("op") != OP_HELLO:
            raise QQBotError(f"expected HELLO, got {hello}")
        interval = float((hello.get("d") or {}).get("heartbeat_interval", 41250)) / 1000
        token = await self.api.tokens.get()
        if self.session_id and self.seq is not None:
            await ws.send(
                json.dumps(
                    {
                        "op": OP_RESUME,
                        "d": {
                            "token": f"QQBot {token}",
                            "session_id": self.session_id,
                            "seq": self.seq,
                        },
                    }
                )
            )
        else:
            await ws.send(
                json.dumps(
                    {
                        "op": OP_IDENTIFY,
                        "d": {
                            "token": f"QQBot {token}",
                            "intents": self.intents,
                            "shard": [0, 1],
                            "properties": {},
                        },
                    }
                )
            )
        heartbeat = asyncio.create_task(self._heartbeat(ws, interval))
        try:
            async for raw in ws:
                if self._stopping:
                    await ws.close()
                    return
                await self._handle(ws, json.loads(raw))
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except (asyncio.CancelledError, ConnectionClosed):
                pass

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self.seq}))

    async def _handle(self, ws: Any, frame: dict) -> None:
        op = frame.get("op")
        if op == OP_RECONNECT:
            await ws.close()
            return
        if op == OP_INVALID_SESSION:
            self.session_id, self.seq = None, None
            await ws.close()
            return
        if op != OP_DISPATCH:
            return
        if frame.get("s") is not None:
            self.seq = int(frame["s"])
        kind = frame.get("t") or ""
        data = frame.get("d") or {}
        match kind:
            case "READY":
                self.session_id = data.get("session_id")
                await self._mark_ready()
            case "RESUMED":
                await self._mark_ready()
            case _:
                result = self.on_dispatch(kind, data)
                if asyncio.iscoroutine(result):
                    await result

    async def _mark_ready(self) -> None:
        self.state = "ready"
        self.last_error = ""
        self.ready.set()
        if self._on_ready is not None:
            result = self._on_ready()
            if asyncio.iscoroutine(result):
                await result
