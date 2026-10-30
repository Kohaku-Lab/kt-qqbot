"""Gateway protocol against a real local WebSocket server."""

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from kt_qqbot.errors import GatewayFatal
from kt_qqbot.gateway import INTENT_GROUP_AND_C2C, Gateway

pytestmark = pytest.mark.timeout(20)


class FakeTokens:
    def __init__(self):
        self.n = 0
        self.invalidated = 0

    async def get(self):
        return f"tok{self.n}"

    def invalidate(self):
        self.invalidated += 1
        self.n += 1


class FakeApi:
    def __init__(self, url):
        self.url = url
        self.tokens = FakeTokens()

    async def gateway_url(self):
        return self.url


class Script:
    """Server behavior per connection: a list of callables run in order."""

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.frames: list[list[dict]] = []

    async def handle(self, ws):
        frames: list[dict] = []
        self.frames.append(frames)
        session = self.sessions.pop(0)
        await session(ws, frames)


async def hello_and_read(ws, frames, interval_ms=30):
    await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}}))
    frames.append(json.loads(await ws.recv()))


async def test_identify_dispatch_heartbeat_resume_and_fatal():
    events = []

    async def first(ws, frames):
        await hello_and_read(ws, frames)
        await ws.send(
            json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S1"}})
        )
        await ws.send(
            json.dumps({"op": 0, "s": 2, "t": "C2C_MESSAGE_CREATE", "d": {"id": "M1"}})
        )
        heartbeat = json.loads(await ws.recv())
        frames.append(heartbeat)
        await ws.close(4900, "server restart")

    async def second(ws, frames):
        await hello_and_read(ws, frames)
        await ws.send(json.dumps({"op": 0, "s": 3, "t": "RESUMED", "d": {}}))
        await ws.close(4004, "bad token")

    async def third(ws, frames):
        await hello_and_read(ws, frames)
        await ws.close(4914, "offline")

    script = Script([first, second, third])
    async with serve(script.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        api = FakeApi(f"ws://127.0.0.1:{port}")
        sleeps = []

        async def fast_sleep(delay):
            sleeps.append(delay)
            await asyncio.sleep(0.01 if delay > 0.02 else delay)

        gateway = Gateway(api, lambda t, d: events.append((t, d)), sleep=fast_sleep)
        with pytest.raises(GatewayFatal):
            await gateway.run()

    identify = script.frames[0][0]
    assert identify["op"] == 2
    assert identify["d"]["token"] == "QQBot tok0"
    assert identify["d"]["intents"] == INTENT_GROUP_AND_C2C
    assert script.frames[0][1] == {"op": 1, "d": 2}
    assert events == [("C2C_MESSAGE_CREATE", {"id": "M1"})]
    resume = script.frames[1][0]
    assert resume == {
        "op": 6,
        "d": {"token": "QQBot tok0", "session_id": "S1", "seq": 2},
    }
    assert api.tokens.invalidated == 1
    third_frame = script.frames[2][0]
    assert third_frame["op"] == 2, "4004 drops the session, so the next is IDENTIFY"
    assert third_frame["d"]["token"] == "QQBot tok1"
    assert gateway.state == "fatal"


async def test_rate_limit_waits_sixty_seconds_and_stop_ends_loop():
    async def limited(ws, frames):
        await hello_and_read(ws, frames)
        await ws.close(4008, "rate limited")

    script = Script([limited])
    async with serve(script.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        sleeps = []
        gateway = None

        async def record_sleep(delay):
            sleeps.append(delay)
            if delay >= 1:
                gateway.stop()

        gateway = Gateway(
            FakeApi(f"ws://127.0.0.1:{port}"), lambda t, d: None, sleep=record_sleep
        )
        await gateway.run()
    assert 60 in sleeps
    assert gateway.state == "stopped"


async def test_backoff_grows_on_repeated_failures():
    class DownApi(FakeApi):
        async def gateway_url(self):
            raise OSError("network down")

    sleeps = []
    gateway = None

    async def record_sleep(delay):
        sleeps.append(delay)
        if len(sleeps) == 8:
            gateway.stop()

    gateway = Gateway(DownApi("x"), lambda t, d: None, sleep=record_sleep)
    await gateway.run()
    assert sleeps == [1, 2, 5, 10, 30, 60, 60, 60]
    assert "network down" in gateway.last_error
