"""qq_send with ``sandbox:`` handles against a real kt-sandbox sandbox in Docker.

Run with ``KT_QQBOT_SANDBOX_LIVE=1`` (needs the ``kt-sandbox[manager]`` package,
a running Docker engine and the image named by ``KT_QQBOT_SANDBOX_IMAGE``,
default ``kt-sandbox:live``). QQ itself is the fake OpenAPI from conftest.
"""

import base64
import os
import struct
import uuid
import zlib
from types import SimpleNamespace

import pytest

from kohakuterrarium.core.trigger_manager import TriggerManager
from kohakuterrarium.modules.tool.base import ToolContext

from kt_qqbot.outbound import MAX_SINGLE_UPLOAD_BYTES
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.tools import QQSendTool
from kt_qqbot.trigger import QQFeedTrigger

from tests.conftest import c2c_event, group_event

pytestmark = [
    pytest.mark.sandbox_live,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        os.environ.get("KT_QQBOT_SANDBOX_LIVE") != "1",
        reason="set KT_QQBOT_SANDBOX_LIVE=1",
    ),
]

IMAGE = os.environ.get("KT_QQBOT_SANDBOX_IMAGE", "kt-sandbox:live")
BIG_BYTES = 6 * 1024 * 1024


def _png() -> bytes:
    """Return a valid 2x2 RGB PNG."""

    def chunk(tag: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(tag + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    rows = b"".join(b"\0" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = _png()


@pytest.fixture
def sandbox():
    """Open a strict sandbox from the live image; remove it afterwards."""
    manager_mod = pytest.importorskip("kt_sandbox.manager.sandboxes")
    engine = pytest.importorskip("kt_sandbox.manager.docker.engine")
    containers = pytest.importorskip("kt_sandbox.manager.docker.containers")
    errors = pytest.importorskip("kt_sandbox.errors")
    try:
        client = engine.connect()
    except errors.SandboxError as exc:
        pytest.skip(f"Docker is not reachable: {exc}")
    if not client.images.list(name=IMAGE):
        client.close()
        pytest.skip(f"image {IMAGE} is missing; build it with kt-sandbox build")
    manager = manager_mod.Manager(client)
    name = f"live-qq-{uuid.uuid4().hex[:8]}"
    try:
        up = manager.up(name, level_name="strict", image=IMAGE)
        container = containers.require(client, name)
        yield SimpleNamespace(name=name, container=container, info=up.info)
    finally:
        try:
            manager.rm(name, force=True)
        finally:
            client.close()


def _write(container, path: str, data: bytes) -> None:
    encoded = base64.b64encode(data).decode()
    code, out = container.exec_run(
        ["sh", "-c", f"printf %s '{encoded}' | base64 -d > {path}"]
    )
    assert code == 0, out


def _shell(container, command: str) -> None:
    code, out = container.exec_run(["sh", "-c", command])
    assert code == 0, out


async def _context(hub, tmp_path, event, data):
    feed = QQFeedTrigger(batch_delay=0, wake="all")
    await feed.attach(hub)
    await feed.start()
    await hub.on_dispatch(event, data)
    for _ in range(5):
        if await feed.wait_for_trigger() is not None:
            break
    manager = TriggerManager(process_event=None)
    manager._triggers["qq_feed"] = feed
    ctx = ToolContext(
        agent_name="bot",
        session=None,
        working_dir=tmp_path,
        agent=SimpleNamespace(trigger_manager=manager),
    )
    return feed, ctx


async def test_qq_send_reads_files_from_a_real_sandbox(
    sandbox, make_hub, fake_api, tmp_path
):
    box, name = sandbox.container, sandbox.name
    _write(box, "/work/chart.png", PNG)
    _write(box, "/work/notes.png", b"just some notes, not a picture\n")
    _write(box, "/work/report.pdf", b"%PDF-1.7\n%fake\n")
    _shell(box, "ln -s /etc/passwd /work/link.png")
    _shell(box, "mkdir -p /work/plots.png")
    _shell(box, f"head -c {BIG_BYTES} /dev/zero > /work/big.png")
    send = QQSendTool()

    hub = make_hub()
    feed, ctx = await _context(hub, tmp_path, EVENT_C2C, c2c_event("C1", "U1", "x"))
    ok = await send.execute({"media": f"sandbox:{name}/chart.png"}, context=ctx)
    assert ok.output.startswith("Sent as m"), ok.error
    upload = fake_api.sent("/files")[-1]
    assert upload["file_type"] == 1
    assert base64.b64decode(upload["file_data"]) == PNG

    by_id = await send.execute(
        {"media": f"sandbox:{box.id[:12]}/chart.png"}, context=ctx
    )
    assert by_id.success, by_id.error
    assert base64.b64decode(fake_api.sent("/files")[-1]["file_data"]) == PNG
    uploads = len(fake_api.sent("/files"))

    wrong = await send.execute({"media": f"sandbox:{name}/notes.png"}, context=ctx)
    assert wrong.error == (
        "NOT SENT: notes.png is not an image (it looks like text). Check the "
        "file, or send it with media_type file."
    )
    link = await send.execute({"media": f"sandbox:{name}/link.png"}, context=ctx)
    assert link.error == (
        f"NOT SENT: /work/link.png in sandbox {name} is a symbolic link to "
        "/etc/passwd; name a regular file instead"
    )
    folder = await send.execute({"media": f"sandbox:{name}/plots.png"}, context=ctx)
    assert folder.error == (
        f"NOT SENT: /work/plots.png in sandbox {name} is a folder; name a file in it"
    )
    big = await send.execute({"media": f"sandbox:{name}/big.png"}, context=ctx)
    assert big.error == (
        f"NOT SENT: /work/big.png in sandbox {name} is {BIG_BYTES} bytes, over the "
        f"{MAX_SINGLE_UPLOAD_BYTES}-byte limit; make a smaller file (compress, "
        "resize or split it) and send that"
    )
    missing = await send.execute({"media": f"sandbox:{name}/gone.png"}, context=ctx)
    assert missing.error.startswith(
        f"NOT SENT: /work/gone.png does not exist in sandbox {name}"
    )
    nobody = await send.execute({"media": "sandbox:nope/x.png"}, context=ctx)
    assert nobody.error == "NOT SENT: no sandbox named 'nope'; see `kt-sandbox ls`"
    assert len(fake_api.sent("/files")) == uploads, "refusals upload nothing"
    await feed.stop()
    await hub.close()

    group_hub = make_hub()
    feed, ctx = await _context(
        group_hub, tmp_path, EVENT_GROUP, group_event("G1", "G1", "P", "x")
    )
    as_file = await send.execute({"media": f"sandbox:{name}/report.pdf"}, context=ctx)
    assert as_file.error == (
        "NOT SENT: QQ group chats do not take file messages. Send it in a "
        "private chat, or as an image or video."
    )
    assert len(fake_api.sent("/files")) == uploads
    image = await send.execute({"media": f"sandbox:{name}/chart.png"}, context=ctx)
    assert image.success, image.error
    upload = fake_api.sent("/files")[-1]
    upload_paths = [p for _, p, _ in fake_api.requests if p.endswith("/files")]
    assert "/groups/G1/" in upload_paths[-1]
    assert upload["file_type"] == 1
    assert base64.b64decode(upload["file_data"]) == PNG
    await feed.stop()
    await group_hub.close()
