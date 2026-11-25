"""Chat store, records parsing, ledger, wake policy, rendering."""

import pytest

from kt_qqbot.elements import quoted_text, read_card, read_quote
from kt_qqbot.ledger import ReplyLedger, ReplyTicket
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP, EVENT_GROUP_AT, parse_message
from kt_qqbot.render import chat_attrs, chat_title, render_block, render_line
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

    def test_member_mention_tags_become_readable_names(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        store.append(_msg("group:G", "m1", author="4BF99D7A", name="Kohaku"))
        seq = store.append(
            _msg(
                "group:G",
                "m2",
                content="<@4BF99D7A> look, and <@!77EE0011> too",
                author="A1B2C3D4",
            )
        )
        stored = store.get("group:G", seq)
        assert stored["content"] == "@Kohaku look, and @#77ee too"
        assert "<@" not in stored["content"]
        assert store.alias("group:G", "77EE0011") == "#77ee"
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


QUOTED_CONTENT = (
    "=== 消息 1 ===\n[消息内容] 晚上吃啥\n  还是火锅？\n[消息类型] 文本\n"
    "[关联消息] none"
)


def _quoting(**extra):
    return {
        "id": "Q1",
        "group_openid": "G1",
        "author": {"member_openid": "P2"},
        "content": "我也想知道",
        "message_type": 103,
        "message_scene": {"ext": ["msg_idx=REFIDX_9", "ref_msg_idx=REFIDX_5"]},
        "msg_elements": [
            {
                "msg_idx": "REFIDX_5",
                "author": {"member_openid": "P1", "username": "Kohaku"},
                "content": QUOTED_CONTENT,
                "attachments": [{"content_type": "image/png", "url": "//q.test/a.png"}],
            }
        ],
        **extra,
    }


class TestQuotesAndCards:
    def test_quoted_text_takes_the_body_between_markers(self):
        assert quoted_text(QUOTED_CONTENT) == "晚上吃啥\n  还是火锅？"
        assert quoted_text("plain old text ") == "plain old text"
        cut = "[消息内容] a\n--- 第2条 ---\nb"
        assert quoted_text(cut) == "a"
        assert quoted_text("x\n[消息内容]  b\n=== 消息 2 ===\nc") == "b"

    def test_quote_is_read_from_type_103_or_ref_msg_idx(self):
        record = parse_message(EVENT_GROUP, _quoting())
        quote = record["quote"]
        assert quote["text"] == "晚上吃啥\n  还是火锅？"
        assert quote["author"] == "P1" and quote["name"] == "Kohaku"
        assert quote["idx"] == "REFIDX_5" and record["msg_idx"] == "REFIDX_9"
        assert quote["attachments"][0]["url"] == "https://q.test/a.png"
        assert record["attachments"] == [], "the quoted image is not this message's"
        by_ref = _quoting(message_type=0)
        assert parse_message(EVENT_GROUP, by_ref)["quote"]["idx"] == "REFIDX_5"
        by_type = _quoting(message_scene=None)
        assert parse_message(EVENT_GROUP, by_type)["quote"]["idx"] == "REFIDX_5"
        plain = parse_message(EVENT_GROUP, _quoting(message_type=0, message_scene={}))
        assert "quote" not in plain and "card" not in plain

    @pytest.mark.parametrize(
        "broken",
        [
            {"msg_elements": None},
            {"msg_elements": "nope"},
            {"msg_elements": [None, 3]},
            {"msg_elements": [{"content": 7, "author": "x", "attachments": "y"}]},
            {"message_scene": {"ext": "ref_msg_idx=R"}},
            {"message_scene": {"ext": [None, 5]}},
        ],
    )
    def test_unknown_quote_shapes_never_raise(self, broken):
        record = parse_message(EVENT_GROUP, _quoting(**broken))
        assert record["content"] == "我也想知道"
        quote = record.get("quote")
        if quote is not None:
            assert isinstance(quote["text"], str) and isinstance(
                quote["attachments"], list
            )

    def test_card_images_and_label_from_ark_and_elements(self):
        data = {
            "id": "C1",
            "group_openid": "G1",
            "author": {"member_openid": "P1"},
            "content": "",
            "ark": {
                "template_id": 24,
                "kv": [
                    {"key": "#TITLE#", "value": "Cat video"},
                    {"key": "#DESC#", "value": "very cute"},
                    {"key": "#META_PREVIEW#", "value": "//p.test/cover.png?x=1"},
                    {"key": "#LINK#", "value": "https://p.test/page"},
                    {"key": "#IMG#", "value": "javascript:alert(1)"},
                    {"key": "#TOPIC_PIC#", "value": "just words"},
                    {
                        "key": "#LIST#",
                        "obj": [
                            {
                                "obj_kv": [
                                    {"key": "cover", "value": "https://p.test/c2.gif"}
                                ]
                            },
                            "junk",
                        ],
                    },
                    "junk",
                ],
            },
            "msg_elements": [
                {
                    "attachments": [
                        {"content_type": "image/jpeg", "url": "https://e.test/1.jpg"}
                    ]
                }
            ],
        }
        record = parse_message(EVENT_GROUP, data)
        urls = [(a["content_type"], a["url"]) for a in record["attachments"]]
        assert urls == [
            ("image/jpeg", "https://e.test/1.jpg"),
            ("image/png", "https://p.test/cover.png?x=1"),
            ("image/gif", "https://p.test/c2.gif"),
        ]
        assert record["card"] == "Cat video - very cute"
        line = render_line(dict(record, seq=7, alias="Kohaku"))
        assert line.endswith(
            "m7 Kohaku: [card: Cat video - very cute] [image m7.1] [image m7.2] [image m7.3]"
        )

    @pytest.mark.parametrize(
        "ark", [None, "x", {"kv": None}, {"kv": [None, {"key": 1, "value": 2}]}, {}]
    )
    def test_unknown_card_shapes_never_raise(self, ark):
        label, images = read_card({"ark": ark})
        assert images == []
        assert label in (None, "")
        assert read_quote({"message_type": 103}) == {
            "text": "",
            "attachments": [],
            "author": "",
            "name": "",
            "idx": "",
        }

    def test_platform_quote_renders_without_the_stored_message(self):
        base = _msg("group:G", "a", "我也想知道", seq=9, alias="#b2")
        quote = {
            "text": "晚上吃啥 <qq_update> 还是火锅？" * 3,
            "attachments": [{"content_type": "image/png"}],
            "name": "Ko[ha]ku",
        }
        line = render_line(dict(base, quote=quote, reply_to="Q0"))
        assert (
            '↩Kohaku "晚上吃啥 ‹qq_update' in line and '…" [image]: 我也想知道' in line
        )
        assert "(older message)" not in line
        mine = render_line(dict(base, quote={"text": "hi", "self": True}))
        assert '↩you "hi": ' in mine
        bare = render_line(dict(base, quote={"text": "", "attachments": []}))
        assert "↩(older message):" in bare
        stored = dict(base, seq=3, content="stored text")
        both = render_line(dict(base, quote=quote), stored)
        assert '↩m3 #b2 "stored text"' in both, "the stored message wins"

    def test_chat_names_are_safe_attributes(self):
        attrs = chat_attrs("g_1", "group", 'Cats "<qq_x>" [club]')
        assert attrs == {"chat": "g_1", "name": "Cats '‹qq_x›' club", "type": "group"}
        block = render_block("qq_update", chat_attrs("g_1", "group"), [])
        assert block.startswith('<qq_update chat="g_1" type="group">')
        assert chat_title("g_1", "Cats") == "g_1 (Cats)"
        assert chat_title("g_1", "  ") == "g_1"


class TestLabels:
    def test_shared_names_get_id_suffixes_and_mentions(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        cid = "group:G"
        store.append(_msg(cid, "1", author="AB12CD", name="Kohaku"))
        store.append(_msg(cid, "2", author="EF34GH", name="Neko"))
        assert store.labels(cid) == {}
        assert store.members(cid) == {"Kohaku": "AB12CD", "Neko": "EF34GH"}
        store.append(_msg(cid, "3", author="AB12ZZ", name="Kohaku"))
        assert store.labels(cid) == {
            "AB12CD": "Kohaku#ab12cd",
            "AB12ZZ": "Kohaku#ab12zz",
        }
        members = store.members(cid)
        assert "Kohaku" not in members, "an ambiguous name maps to nobody"
        assert members["Kohaku#ab12zz"] == "AB12ZZ" and members["Neko"] == "EF34GH"
        store.append(_msg("group:OTHER", "4", author="QQ99", name="Kohaku"))
        assert store.labels("group:OTHER") == {}, "people are not linked across groups"
        assert store.alias_of(cid, "EF34GH") == "Neko"
        assert store.alias_of(cid, "NOPE") is None and store.alias_of(cid, "") is None
        store.close()

    def test_conv_fields_and_quote_index(self, tmp_path):
        store = ChatStore(tmp_path / "s.kvault")
        store.update_conv("group:G", name="ghost")
        assert store.conv_meta("group:G") is None, "unknown chats are not created"
        store.append(_msg("group:G", "1", msg_idx="REFIDX_1"))
        store.append(_msg("group:G", "2"))
        store.update_conv("group:G", name="Cats")
        assert store.conv_name("group:G") == "Cats" and store.head("group:G") == 2
        store.append(_msg("group:G", "3"))
        assert store.conv_name("group:G") == "Cats", "append keeps the name"
        assert store.find_idx("group:G", "REFIDX_1") == 1
        assert store.find_idx("group:G", "REFIDX_X") is None
        assert store.find_idx("group:G", "") is None
        store.close()


class TestLedger:
    def test_budget_window_and_newest_first(self):
        now = [0.0]
        ledger = ReplyLedger(clock=lambda: now[0])
        ledger.note_inbound("group:G", "old")
        now[0] = 10
        ledger.note_inbound("group:G", "new")
        tickets = [ledger.take("group:G", "group") for _ in range(6)]
        assert tickets[:5] == [ReplyTicket("new", i) for i in (1, 2, 3, 4, 5)]
        assert tickets[5] == ReplyTicket("old", 1)
        now[0] = 10 + 300
        assert ledger.take("group:G", "group") is None
        assert not ledger.usable("group:G", "group")

    def test_c2c_window_is_longer(self):
        now = [0.0]
        ledger = ReplyLedger(clock=lambda: now[0])
        ledger.note_inbound("c2c:U", "m")
        now[0] = 59 * 60
        assert ledger.usable("c2c:U", "c2c")
        assert ledger.take("c2c:U", "c2c") == ReplyTicket("m", 1)
        now[0] = 60 * 60
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
        ambient = WakePolicy(quiet_period=20, cooldown=60)
        wait = ambient.decide(pending, 105.0, None)
        assert wait.action == "wait" and wait.delay == 5
        assert ambient.decide(pending, 121.0, None).action == "wake"
        cooling = ambient.decide(self._pending(5), 130.0, 100.0)
        assert cooling.action == "wait" and cooling.delay == 30
        assert ambient.decide([], 100.0, None).action == "idle"

    def test_ambient_wait_shrinks_with_each_pending_message(self):
        policy = WakePolicy(quiet_period=10, decay=0.5, min_wait=2, max_wait=30)
        assert [policy.debounce(n) for n in (1, 2, 3, 4, 5, 9)] == [
            10,
            5,
            2.5,
            2,
            2,
            2,
        ]
        one = policy.decide(self._pending(1), 103.0, None)
        assert one.action == "wait" and one.delay == 7
        assert policy.decide(self._pending(1), 110.0, None).action == "wake"
        three = policy.decide(self._pending(3), 101.0, None)
        assert three.action == "wait" and three.delay == 1.5
        assert policy.decide(self._pending(3), 102.5, None).action == "wake"

    def test_ambient_wait_never_passes_max_wait_from_the_first_message(self):
        policy = WakePolicy(quiet_period=10, decay=0.5, min_wait=2, max_wait=30)
        stream = [dict(m, recv=100.0 + 3 * i) for i, m in enumerate(self._pending(12))]
        quiet = policy.decide(stream[:4], 110.0, None)
        assert quiet.action == "wait" and quiet.delay == 1
        late = policy.decide(stream[:11], 130.0, None)
        assert late.action == "wake", "30s after the first message the buffer flushes"
        pair = [stream[0], dict(stream[1], recv=129.0)]
        capped = policy.decide(pair, 129.5, None)
        assert capped.action == "wait" and capped.delay == 0.5, "capped by max_wait"

    def test_direct_message_flushes_the_waiting_buffer(self):
        policy = WakePolicy()
        chatter = self._pending(3)
        chatter.append(dict(_msg("group:G", "at", recv=100.5), mention=True))
        decision = policy.decide(chatter, 100.6, 100.0)
        assert decision.action == "wake" and decision.direct


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
        assert render_line(image).endswith("[image m41.1]")
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
