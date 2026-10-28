"""Chat store, records parsing, ledger, wake policy, rendering."""

from kt_qqbot.ledger import ReplyLedger, ReplyTicket
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP, EVENT_GROUP_AT, parse_message
from kt_qqbot.render import render_block, render_line
from kt_qqbot.store import ChatStore
from kt_qqbot.wake import WakePolicy


def _msg(cid, msg_id, content="hi", author="A1B2C3D4", **extra):
    kind = cid.partition(":")[0]
    return {
        "conv": cid,
        "kind": kind,
        "msg_id": msg_id,
        "author": author,
        "name": "",
        "content": content,
        "ts": 1000.0,
        "attachments": [],
        "mention": False,
        "reply_to": "",
        "self": False,
        **extra,
    }


class TestChatStore:
    def test_append_dedup_range_and_persistence(self, tmp_path):
        path = tmp_path / "s.kvault"
        store = ChatStore(path)
        assert store.append(_msg("group:G1", "m1")) == 1
        assert store.append(_msg("group:G1", "m2")) == 2
        assert store.append(_msg("group:G1", "m1")) is None
        assert store.append(_msg("c2c:U1", "m3")) == 1
        assert [m["msg_id"] for m in store.range("group:G1", 0)] == ["m1", "m2"]
        assert [m["msg_id"] for m in store.range("group:G1", 1)] == ["m2"]
        assert store.find("m2") == ("group:G1", 2)
        assert sorted(store.conversations()) == ["c2c:U1", "group:G1"]
        store.set_cursor("sub", "group:G1", 2)
        store.close()
        again = ChatStore(path)
        assert again.head("group:G1") == 2
        assert again.cursor("sub", "group:G1") == 2
        assert again.append(_msg("group:G1", "m4")) == 3
        again.close()

    def test_retention_prunes_log_and_ids(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault", retention=3)
        for i in range(1, 6):
            store.append(_msg("group:G", f"id{i}"))
        assert store.low("group:G") == 3
        assert [m["seq"] for m in store.range("group:G", 0)] == [3, 4, 5]
        assert store.find("id1") is None and store.find("id5") == ("group:G", 5)
        assert [m["seq"] for m in store.recent("group:G", 2)] == [4, 5]
        assert [m["seq"] for m in store.recent("group:G", 10, before=5)] == [3, 4]
        store.close()

    def test_handles_are_unique_and_resolvable(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        store.append(_msg("group:ABCDEF111", "a"))
        store.append(_msg("group:ABCDEF222", "b"))
        h1 = store.handle("group:ABCDEF111")
        h2 = store.handle("group:ABCDEF222")
        assert h1 == "g_abcdef" and h2 != h1 and h2.startswith("g_abcdef")
        assert store.resolve_handle(h2) == "group:ABCDEF222"
        assert store.resolve_handle("group:ABCDEF111") == "group:ABCDEF111"
        assert store.resolve_handle("g_nothing") is None
        store.close()

    def test_aliases_stable_named_and_distinct(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        assert store.alias("group:G", "AAAA1111") == "#aaaa"
        assert store.alias("group:G", "AAAA2222") == "#aaaa22"
        assert store.alias("group:G", "AAAA1111") == "#aaaa"
        assert store.alias("group:G", "AAAA1111", "Kohaku") == "Kohaku"
        assert store.alias("group:G", "AAAA1111") == "Kohaku"
        store.close()

    def test_cursor_never_moves_back_and_prefixes_do_not_collide(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        store.set_cursor("a", "group:G", 5)
        store.set_cursor("a", "group:G", 3)
        store.set_cursor("a:group", "c2c:U", 1)
        assert store.cursor("a", "group:G") == 5
        assert store.subscribed("a") == ["group:G"]
        assert store.subscribed("a:group") == ["c2c:U"]
        store.close()


class TestRecords:
    def test_c2c_group_at_and_plain_group(self):
        c2c = parse_message(
            EVENT_C2C,
            {
                "id": "M1",
                "author": {"user_openid": "U1"},
                "content": " hello ",
                "attachments": [
                    {"content_type": "image/png", "url": "gchat.qpic.cn/x.png"}
                ],
            },
        )
        assert c2c["conv"] == "c2c:U1" and c2c["mention"] is True
        assert c2c["content"] == "hello"
        assert c2c["attachments"][0]["url"] == "https://gchat.qpic.cn/x.png"
        at = parse_message(
            EVENT_GROUP_AT,
            {
                "id": "M2",
                "group_openid": "G1",
                "author": {"member_openid": "P1"},
                "content": "<@!BOT> hi",
                "message_reference": {"message_id": "M0"},
            },
        )
        assert at["conv"] == "group:G1" and at["author"] == "P1"
        assert at["mention"] is True and at["content"] == "hi"
        assert at["reply_to"] == "M0"
        plain = parse_message(
            EVENT_GROUP, {"id": "M3", "group_openid": "G1", "author": {}}
        )
        assert plain["mention"] is False
        assert parse_message("GUILD_CREATE", {"id": "x"}) is None
        assert parse_message(EVENT_C2C, {"author": {"user_openid": "U"}}) is None


class TestLedger:
    def test_budget_window_and_newest_first(self):
        now = [0.0]
        ledger = ReplyLedger(clock=lambda: now[0])
        ledger.note_inbound("group:G", "old")
        now[0] = 10
        ledger.note_inbound("group:G", "new")
        tickets = [ledger.take("group:G", "group") for _ in range(5)]
        assert tickets[:4] == [ReplyTicket("new", i) for i in (1, 2, 3, 4)]
        assert tickets[4] == ReplyTicket("old", 1)
        now[0] = 10 + 300
        assert ledger.take("group:G", "group") is None
        assert not ledger.usable("group:G", "group")

    def test_c2c_window_is_longer(self):
        now = [0.0]
        ledger = ReplyLedger(clock=lambda: now[0])
        ledger.note_inbound("c2c:U", "m")
        now[0] = 29 * 60
        assert ledger.usable("c2c:U", "c2c")
        assert ledger.take("c2c:U", "c2c") == ReplyTicket("m", 1)
        now[0] = 30 * 60
        assert ledger.take("c2c:U", "c2c") is None
        assert ledger.take("c2c:other", "c2c") is None


class TestWakePolicy:
    def _pending(self, n, recv=100.0, **extra):
        return [dict(_msg("group:G", f"x{i}", recv=recv), **extra) for i in range(n)]

    def test_direct_always_wakes(self):
        for mode in ("mention", "all", "ambient"):
            policy = WakePolicy(mode=mode)
            decision = policy.decide(self._pending(1, mention=True), 100.0, 99.0)
            assert decision.action == "wake" and decision.direct
        reply = self._pending(1, to_self=True)
        assert WakePolicy(mode="mention").decide(reply, 100.0, None).action == "wake"

    def test_modes_for_ambient_chat(self):
        pending = self._pending(2)
        assert WakePolicy(mode="mention").decide(pending, 100.0, None).action == "idle"
        assert WakePolicy(mode="all").decide(pending, 100.0, None).action == "wake"
        ambient = WakePolicy(quiet_period=20, every_n=3, cooldown=60)
        wait = ambient.decide(pending, 105.0, None)
        assert wait.action == "wait" and wait.delay == 15
        assert ambient.decide(pending, 121.0, None).action == "wake"
        assert ambient.decide(self._pending(3), 101.0, None).action == "wake"
        cooling = ambient.decide(self._pending(5), 130.0, 100.0)
        assert cooling.action == "wait" and cooling.delay == 30
        assert ambient.decide([], 100.0, None).action == "idle"


class TestRender:
    def test_lines_and_block(self):
        base = _msg("group:G", "a", "晚上吃啥", seq=41, alias="#a3f9")
        assert render_line(base).endswith("m41 #a3f9: 晚上吃啥")
        mention = dict(base, mention=True, seq=42)
        assert "m42 #a3f9 [@you]:" in render_line(mention)
        quoted = dict(base, seq=40, self=True, content="我请客" * 30)
        line = render_line(dict(base, seq=43), quoted)
        assert '↩m40 you "' in line and "…" in line
        image = dict(base, attachments=[{"content_type": "image/jpeg"}])
        assert render_line(image).endswith("[image]")
        mine = dict(base, self=True, seq=44)
        assert "m44 [you]:" in render_line(mine)
        block = render_block("qq_update", {"chat": "g_1", "type": "group"}, ["x"])
        assert block == '<qq_update chat="g_1" type="group">\nx\n</qq_update>'


def test_out_of_order_old_message_does_not_hide_newer_slots():
    now = [1000.0]
    ledger = ReplyLedger(clock=lambda: now[0])
    ledger.note_inbound("group:G", "new", at=990.0)
    ledger.note_inbound("group:G", "late", at=100.0)
    assert ledger.take("group:G", "group") == ReplyTicket("new", 1)
