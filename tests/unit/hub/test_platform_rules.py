"""Official QQ rules: reply budgets, full-message-mode mentions, no active sends."""

import json
import os
import stat

import pytest

from kt_qqbot.gateway import Gateway
from kt_qqbot.hub import CLOSED_WINDOW
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
