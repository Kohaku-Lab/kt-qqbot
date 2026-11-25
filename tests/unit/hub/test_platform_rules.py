"""Official QQ rules: reply budgets, full-message-mode mentions, no active sends."""

import json
import os
import stat

import pytest

from kt_qqbot import hints
from kt_qqbot.errors import QQApiError
from kt_qqbot.gateway import Gateway
from kt_qqbot.hub import CLOSED_WINDOW
from kt_qqbot.api import FILE_IMAGE
from kt_qqbot.outbound import MediaItem, split_text
from kt_qqbot.ledger import ReplyLedger, ReplyTicket
from kt_qqbot.paths import root_dir
from kt_qqbot.records import (
    EVENT_C2C,
    EVENT_GROUP,
    EVENT_GROUP_AT,
    mentions_bot,
    parse_message,
)
from kt_qqbot.trigger import QQFeedTrigger

from tests.conftest import c2c_event, group_event

BOT = "BOT123"


# ── 1. reply budgets ──────────────────────────────────────────────


def test_official_budgets_per_kind():
    now = [0.0]
    ledger = ReplyLedger(clock=lambda: now[0])
    ledger.note_inbound("group:G", "g")
    ledger.note_inbound("c2c:U", "u")
    group = [ledger.take("group:G", "group") for _ in range(6)]
    assert group[:5] == [ReplyTicket("g", i) for i in range(1, 6)] and group[5] is None
    c2c = [ledger.take("c2c:U", "c2c") for _ in range(5)]
    assert c2c[:4] == [ReplyTicket("u", i) for i in range(1, 5)] and c2c[4] is None
    ledger.note_inbound("c2c:V", "v")
    now[0] = 59 * 60
    assert ledger.usable("c2c:V", "c2c")
    now[0] = 60 * 60
    assert not ledger.usable("c2c:V", "c2c")
    ledger.note_inbound("group:H", "h", at=now[0])
    now[0] += 299
    assert ledger.usable("group:H", "group")
    now[0] += 1
    assert not ledger.usable("group:H", "group")
    custom = ReplyLedger(max_replies={"group": 2}, windows={"group": 10})
    custom.note_inbound("group:G", "g")
    assert [custom.take("group:G", "group") for _ in range(3)][2] is None


async def test_every_received_message_is_a_slot_source(make_hub):
    hub = make_hub()
    await hub.on_dispatch(EVENT_GROUP, group_event("plain", "G1", "P", "no mention"))
    await hub.on_dispatch(EVENT_GROUP_AT, group_event("at", "G2", "P", "hi"))
    await hub.on_dispatch(EVENT_C2C, c2c_event("dm", "U1", "hey"))
    assert hub.ledger.free_slots("group:G1", "group") == 5
    assert hub.ledger.free_slots("group:G2", "group") == 5
    assert hub.ledger.free_slots("c2c:U1", "c2c") == 4
    await hub.close()


# ── 2. mentions in full-message mode ──────────────────────────────


@pytest.mark.parametrize(
    ("data", "bot_id", "expected"),
    [
        ({"mentions": [{"is_you": True}]}, "", True),
        ({"mentions": [{"id": BOT}]}, BOT, True),
        ({"mentions": [{"member_openid": BOT}]}, BOT, True),
        ({"mentions": [{"id": "OTHER"}]}, BOT, False),
        ({"content": f"<@!{BOT}> hi"}, BOT, True),
        ({"content": f"<@{BOT}> hi"}, BOT, True),
        ({"content": "<@!OTHER> hi"}, BOT, False),
        ({"content": f"<@!{BOT}> hi"}, "", False),
        ({}, BOT, False),
    ],
)
def test_mention_detection_shapes(data, bot_id, expected):
    assert mentions_bot(data, bot_id) is expected


def test_full_message_mode_parse():
    data = group_event("M", "G", "P", f"<@!{BOT}> look <@!OTHER>")
    record = parse_message(EVENT_GROUP, data, bot_id=BOT)
    assert record["mention"] is True
    assert record["content"] == "look <@!OTHER>", "only the bot's own tag is removed"
    plain = parse_message(EVENT_GROUP, group_event("N", "G", "P", "chat"), bot_id=BOT)
    assert plain["mention"] is False
    at = parse_message(EVENT_GROUP_AT, group_event("A", "G", "P", "<@!X> yo"))
    assert at["mention"] is True and at["content"] == "yo"


async def test_hub_uses_the_ready_bot_id_and_dedup_keeps_the_mention(make_hub):
    hub = make_hub()
    hub.gateway.bot_user = {"id": BOT, "username": "kt"}
    await hub.on_dispatch(EVENT_GROUP, group_event("M1", "G", "P", f"<@!{BOT}> hi"))
    assert hub.store.get("group:G", 1)["mention"] is True
    await hub.on_dispatch(EVENT_GROUP, group_event("M2", "G", "P", "plain"))
    await hub.on_dispatch(EVENT_GROUP_AT, group_event("M2", "G", "P", "plain"))
    assert hub.store.get("group:G", 2)["mention"] is True
    await hub.close()


async def test_gateway_records_ready_user():
    gateway = Gateway(api=None, on_dispatch=lambda t, d: None)
    await gateway._handle(
        None,
        {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "S", "user": {"id": BOT}}},
    )
    assert gateway.bot_user == {"id": BOT}


async def test_debug_raw_events_capture(make_hub):
    hub = make_hub()
    hub.capture_raw(2)
    for i in range(3):
        await hub.on_dispatch(EVENT_C2C, c2c_event(f"R{i}", "U", f"raw {i}"))
    files = list((root_dir() / "debug").glob("raw-*.jsonl"))
    assert len(files) == 1
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(files[0]).st_mode) == 0o600
    text = files[0].read_text(encoding="utf-8")
    lines = [json.loads(x) for x in text.splitlines()]
    assert [x["t"] for x in lines] == [EVENT_C2C, EVENT_C2C]
    assert lines[0]["d"]["content"] == "raw 0" and set(lines[0]) == {"t", "d"}
    assert "tok" not in text
    await hub.close()


async def test_feed_option_starts_raw_capture(make_hub):
    hub = make_hub()
    feed = QQFeedTrigger(debug_raw_events=3)
    await feed.attach(hub)
    await feed.start()
    assert hub._raw_left == 3
    await feed.stop()
    await hub.close()


# ── 3. no active messages ─────────────────────────────────────────


async def test_closed_window_never_calls_the_api(make_hub, fake_api):
    hub = make_hub()
    await hub.ingest(
        {
            "conv": "group:G",
            "kind": "group",
            "msg_id": "OLD",
            "author": "P",
            "content": "x",
            "ts": 0,
            "recv": 0,
            "self": False,
        }
    )
    result = await hub.send("group:G", "late")
    assert not result.sent and result.reason == CLOSED_WINDOW
    assert "5 min" in CLOSED_WINDOW and "60 min" in CLOSED_WINDOW
    assert fake_api.sent() == []
    await hub.close()


@pytest.mark.parametrize("code", [40034128, 40034005, 304103])
async def test_slot_errors_close_the_slot(make_hub, fake_api, code):
    hub = make_hub()
    await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G", "P", "x"))
    fake_api.fail_codes = [code]
    result = await hub.send("group:G", "hi")
    assert not result.sent and result.reason == CLOSED_WINDOW
    assert hub.ledger.free_slots("group:G", "group") == 0
    assert hub.store.slots()["G1"]["used"] == 5
    await hub.close()


async def test_duplicate_seq_retries_with_the_next_seq(make_hub, fake_api):
    hub = make_hub()
    await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G", "P", "x"))
    fake_api.fail_codes = [40054005]
    result = await hub.send("group:G", "hi")
    assert result.sent
    assert [b["msg_seq"] for b in fake_api.sent()] == [1, 2]
    fake_api.fail_codes = [40054005, 40054005]
    again = await hub.send("group:G", "twice")
    assert not again.sent, "only one retry"
    assert (
        again.reason == hints.DUPLICATE_SEQ and "Send the same message" in again.reason
    )
    await hub.close()


async def test_typing_never_takes_the_last_reply(make_hub, fake_api):
    hub = make_hub()
    await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U", "hi"))
    for _ in range(3):
        assert (await hub.send("c2c:U", "x")).sent
    await hub.typing("c2c:U")
    assert all(b["msg_type"] != 6 for b in fake_api.sent())
    last = await hub.send("c2c:U", "final")
    assert last.sent and fake_api.sent()[-1]["msg_seq"] == 4
    await hub.typing("group:G")
    await hub.close()


# ── long text and failure reasons ─────────────────────────────────


def test_split_text_prefers_paragraphs_then_sentences_then_spaces():
    text = "第一段。" * 5 + "\n\n" + "第二段很长。" * 5
    assert split_text(text, 40) == ["第一段。" * 5, "第二段很长。" * 5]
    sentences = split_text("一二三四五。六七八九十！甲乙丙丁戊", 12)
    assert sentences == ["一二三四五。六七八九十！", "甲乙丙丁戊"]
    words = split_text("alpha beta gamma delta epsilon", 12)
    assert all(len(w) <= 12 for w in words) and " ".join(words) == (
        "alpha beta gamma delta epsilon"
    )
    assert split_text("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_text("Dr. Who said hi. Then left.", 18)[0] == "Dr. Who said hi."
    assert split_text("short", 100) == ["short"]
    assert split_text("  ", 10) == []
    tiny = split_text("a。" + "b" * 30, 12)
    assert tiny[0] == "a。" + "b" * 10, "a boundary that leaves a tiny piece is skipped"


async def test_long_text_goes_out_as_several_replies(make_hub, fake_api):
    hub = make_hub()
    await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G", "P", "x"))
    text = "第一段。\n\n" + "第二段。" * 3
    result = await hub.send("group:G", text, reply_to_seq=1, max_chars=8)
    assert result.sent and len(result.seqs) == 3
    bodies = fake_api.sent()
    assert [b["content"] for b in bodies] == [
        "第一段。",
        "第二段。第二段。",
        "第二段。",
    ]
    assert all(len(b["content"]) <= 8 for b in bodies)
    assert bodies[0]["message_reference"] == {"message_id": "G1"}
    assert all("message_reference" not in b for b in bodies[1:])
    assert [b["msg_seq"] for b in bodies] == [1, 2, 3]
    assert [hub.store.get("group:G", s)["content"] for s in result.seqs] == [
        b["content"] for b in bodies
    ]
    await hub.close()


async def test_every_hub_failure_says_what_to_do_next(make_hub, fake_api, monkeypatch):
    hub = make_hub()
    for i in range(3):
        await hub.on_dispatch(EVENT_GROUP, group_event(f"G{i}", "G", "P", "x"))
    hub.store.retention = 2
    await hub.on_dispatch(EVENT_GROUP, group_event("G3", "G", "P", "x"))
    old = await hub.send("group:G", "hi", reply_to_seq=1)
    assert "older than the stored log" in old.reason
    assert old.reason.endswith("Send again without reply_to.")
    missing = await hub.send("group:G", "hi", reply_to_seq=99)
    assert missing.reason == "m99 is not in this chat. Send again without reply_to."
    fake_api.fail_send = "content not allowed"
    refused = await hub.send("group:G", "hi")
    assert "QQ refused the message (code 304003: content not allowed)" in refused.reason
    assert "Rephrase it" in refused.reason
    fake_api.fail_send = None
    fake_api.fail_codes = [40054010]
    link = await hub.send("group:G", "see https://x")
    assert link.reason.endswith("Send it again without the link.")
    fake_api.fail_upload = "bad image"
    item = MediaItem("image", FILE_IMAGE, "a.png", data=b"png")
    upload = await hub.send("group:G", "", media=[item])
    assert upload.reason.startswith(
        "QQ refused the media (bad image). Send a different"
    )
    fake_api.fail_upload = None

    async def offline(*args, **kwargs):
        raise QQApiError(0, None, "connect timeout")

    monkeypatch.setattr(hub.api, "send_message", offline)
    down = await hub.send("group:G", "hi")
    assert down.reason.endswith("Try once more in a minute.")

    async def server(*args, **kwargs):
        raise QQApiError(502, None, "bad gateway")

    monkeypatch.setattr(hub.api, "send_message", server)
    assert "server error" in (await hub.send("group:G", "hi")).reason
    hub.unbound = True
    hub._creds_checked_at = float("inf")
    unbound = await hub.send("group:G", "hi")
    assert unbound.reason == hints.UNBOUND and "kt-qqbot login" in unbound.reason
    assert CLOSED_WINDOW.endswith("Wait for the next message, then send.")
    await hub.close()
