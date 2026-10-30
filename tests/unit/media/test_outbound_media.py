"""Outbound media: type inference, uploads per file_type, qq_send media rules."""

import builtins
from types import SimpleNamespace

import pytest

from kohakuterrarium.core.trigger_manager import TriggerManager
from kohakuterrarium.modules.tool.base import ToolContext

from kt_qqbot import outbound
from kt_qqbot.api import FILE_FILE, FILE_IMAGE, FILE_VIDEO, FILE_VOICE
from kt_qqbot.errors import MediaError
from kt_qqbot.outbound import MAX_SINGLE_UPLOAD_BYTES, MediaItem, infer_type
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP
from kt_qqbot.tools import QQSendTool
from kt_qqbot.trigger import QQFeedTrigger

from tests.conftest import c2c_event, group_event

pytestmark = pytest.mark.timeout(20)

SILK = b"\x02#!SILK_V3" + b"\x00" * 16


async def _setup(hub, tmp_path, event, data):
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


def test_infer_type_from_extension_and_mime():
    assert infer_type("a/cat.PNG") == "image"
    assert infer_type("https://x.test/v.mp4?sig=1") == "video"
    for name in ("a.silk", "b.slk", "c.mp3", "d.wav", "e.amr"):
        assert infer_type(name) == "voice", name
    assert infer_type("report.pdf") == "file"
    assert infer_type("noext", mime="image/webp") == "image"
    assert infer_type("noext") == "file"


class TestHubMediaPayloads:
    async def test_each_file_type_uploads_then_sends_msg_type_7(
        self, make_hub, fake_api
    ):
        hub = make_hub()
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "hi"))
        items = [
            MediaItem("image", FILE_IMAGE, "a.png", data=b"img"),
            MediaItem("video", FILE_VIDEO, "v.mp4", url="https://x.test/v.mp4"),
            MediaItem("voice", FILE_VOICE, "s.silk", data=SILK),
            MediaItem("file", FILE_FILE, "r.pdf", data=b"pdf"),
        ]
        result = await hub.send("c2c:U1", "", media=items)
        assert result.sent and len(result.seqs) == 4
        uploads = fake_api.sent("/files")
        assert [u["file_type"] for u in uploads] == [1, 2, 3, 4]
        assert all(u["srv_send_msg"] is False for u in uploads)
        assert uploads[1]["url"] == "https://x.test/v.mp4"
        assert "file_data" in uploads[0] and "url" not in uploads[0]
        messages = fake_api.sent()
        assert [m["msg_type"] for m in messages] == [7, 7, 7, 7]
        assert all(m["media"] == {"file_info": "FILEINFO"} for m in messages)
        assert [m["msg_seq"] for m in messages] == [1, 2, 3, 4]
        assert all(m["msg_id"] == "C1" for m in messages)
        await hub.close()

    async def test_text_then_media_share_the_passive_budget(self, make_hub, fake_api):
        hub = make_hub()
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "hi"))
        image = MediaItem("image", FILE_IMAGE, "a.png", data=b"img")
        result = await hub.send("c2c:U1", "look", media=[image], reply_to_seq=1)
        assert result.passive and len(result.seqs) == 2
        text, media_msg = fake_api.sent()
        assert text["msg_type"] == 0 and text["content"] == "look"
        assert text["message_reference"] == {"message_id": "C1"}
        assert media_msg["msg_type"] == 7 and "message_reference" not in media_msg
        assert (text["msg_seq"], media_msg["msg_seq"]) == (1, 2)
        assert hub.store.slots()["C1"]["used"] == 2
        assert "[image: a.png]" in hub.store.get("c2c:U1", 3)["content"]
        await hub.close()


class TestQQSendMedia:
    async def test_list_in_order_relative_paths_and_json_string(
        self, make_hub, fake_api, tmp_path
    ):
        hub = make_hub()
        feed, ctx = await _setup(hub, tmp_path, EVENT_C2C, c2c_event("C1", "U1", "x"))
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "gen.png").write_bytes(b"png")
        (tmp_path / "notes.pdf").write_bytes(b"pdf")
        send = QQSendTool()
        result = await send.execute(
            {"message": "here", "media": ["out/gen.png", "notes.pdf"]}, context=ctx
        )
        assert result.output.startswith("Sent as m"), result
        assert [u["file_type"] for u in fake_api.sent("/files")] == [1, 4]
        assert [m["msg_type"] for m in fake_api.sent()] == [0, 7, 7]
        await hub.on_dispatch(EVENT_C2C, c2c_event("C2", "U1", "more please"))
        await feed.wait_for_trigger()
        again = await send.execute(
            {"message": "", "media": ("notes.pdf", "out/gen.png")}, context=ctx
        )
        assert again.success, again.error
        assert [u["file_type"] for u in fake_api.sent("/files")][-2:] == [4, 1]
        json_text = await send.execute(
            {"media": '["notes.pdf", "out/gen.png"]'}, context=ctx
        )
        assert "not found" in json_text.error, "a string is one path, never a list"
        forced = await send.execute(
            {"media": "notes.pdf", "media_type": "image"}, context=ctx
        )
        assert forced.success and fake_api.sent("/files")[-1]["file_type"] == 1
        await feed.stop()
        await hub.close()

    async def test_rejections(self, make_hub, fake_api, tmp_path, monkeypatch):
        hub = make_hub()
        feed, ctx = await _setup(hub, tmp_path, EVENT_C2C, c2c_event("C1", "U1", "x"))
        send = QQSendTool()
        insecure = await send.execute({"media": "http://a.test/x.png"}, context=ctx)
        assert "https" in insecure.error
        missing = await send.execute({"media": "nope.png"}, context=ctx)
        assert "not found" in missing.error
        bad_type = await send.execute(
            {"media": "https://a.test/x", "media_type": "sticker"}, context=ctx
        )
        assert "media_type" in bad_type.error
        big = tmp_path / "big.mp4"
        big.write_bytes(b"\0" * (MAX_SINGLE_UPLOAD_BYTES + 1))
        too_big = await send.execute({"media": "big.mp4"}, context=ctx)
        assert "NOT SENT" in too_big.error and "MB" in too_big.error
        assert not fake_api.sent("/files"), "nothing uploads when a check fails"
        await feed.stop()
        await hub.close()

    async def test_voice_needs_silk_or_the_codec(
        self, make_hub, fake_api, tmp_path, monkeypatch
    ):
        hub = make_hub()
        feed, ctx = await _setup(hub, tmp_path, EVENT_C2C, c2c_event("C1", "U1", "x"))
        (tmp_path / "hi.silk").write_bytes(SILK)
        (tmp_path / "hi.wav").write_bytes(b"RIFF....WAVE")
        real_import = builtins.__import__

        def no_codec(name, *args, **kwargs):
            if name.startswith("graiax"):
                raise ImportError("no codec")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_codec)
        send = QQSendTool()
        ok = await send.execute({"media": "hi.silk"}, context=ctx)
        assert ok.success and fake_api.sent("/files")[-1]["file_type"] == 3
        refused = await send.execute({"media": "hi.wav"}, context=ctx)
        assert refused.error == ("NOT SENT: voice needs silk; install kt-qqbot[voice]")
        monkeypatch.setattr(builtins, "__import__", real_import)

        async def fake_encode(data):
            return SILK

        monkeypatch.setattr(outbound, "_encode_silk", fake_encode)
        converted = await send.execute({"media": "hi.wav"}, context=ctx)
        assert converted.success
        await feed.stop()
        await hub.close()

    async def test_group_files_are_refused(self, make_hub, fake_api, tmp_path):
        hub = make_hub()
        feed, ctx = await _setup(
            hub, tmp_path, EVENT_GROUP, group_event("G1", "G1", "P", "x")
        )
        (tmp_path / "r.pdf").write_bytes(b"pdf")
        result = await QQSendTool().execute({"media": "r.pdf"}, context=ctx)
        assert "group" in result.error and "file" in result.error
        allowed = await QQSendTool(allow_group_files=True).execute(
            {"media": "r.pdf"}, context=ctx
        )
        assert allowed.success
        await feed.stop()
        await hub.close()


def test_chunked_upload_seam_is_typed():
    with pytest.raises(MediaError, match="chunked"):
        outbound.check_size(MediaItem("video", FILE_VIDEO, "v", data=b"\0" * 10), 5)


async def test_failed_upload_keeps_the_slot_and_reports_partial_send(
    make_hub, fake_api, tmp_path
):
    hub = make_hub()
    feed, ctx = await _setup(hub, tmp_path, EVENT_C2C, c2c_event("C1", "U1", "x"))
    (tmp_path / "a.png").write_bytes(b"png")
    fake_api.fail_upload = "file too big"
    result = await QQSendTool().execute(
        {"message": "caption", "media": "a.png"}, context=ctx
    )
    assert result.output.startswith("PARTLY SENT as m2; NOT SENT the rest:")
    assert "file too big" in result.output and result.exit_code == 1
    assert hub.store.slots()["C1"]["used"] == 1, "a failed upload uses no slot"
    await feed.stop()
    await hub.close()
