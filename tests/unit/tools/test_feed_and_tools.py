"""Hub ingest/send, the qq_feed trigger, and the qq_send / qq_history tools."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from kohakuterrarium.core.trigger_manager import TriggerManager
from kohakuterrarium.modules.tool.base import ToolContext

from kt_qqbot import hub as hub_registry
from kt_qqbot import media
from kt_qqbot.auth.credentials import Credentials, CredentialStore
from kt_qqbot.errors import CredentialsMissing
from kt_qqbot.hub import CLOSED_WINDOW
from kt_qqbot.records import EVENT_C2C, EVENT_GROUP, EVENT_GROUP_AT
from kt_qqbot.tools import QQHistoryTool, QQSendTool, QQWatchTool
from kt_qqbot.trigger import QQFeedTrigger, feeds_of

from tests.conftest import APP_ID, SECRET, c2c_event, group_event

pytestmark = pytest.mark.timeout(20)


async def _feed(hub, **options):
    options.setdefault("batch_delay", 0)
    options.setdefault("wake", "all")
    trigger = QQFeedTrigger(**options)
    await trigger.attach(hub)
    await trigger.start()
    return trigger


def _context(tmp_path, *triggers):
    manager = TriggerManager(process_event=None)
    for i, trig in enumerate(triggers):
        manager._triggers[f"t{i}"] = trig
    agent = SimpleNamespace(trigger_manager=manager)
    return ToolContext(
        agent_name="bot", session=None, working_dir=tmp_path, agent=agent
    )


async def _next(trigger, tries=5):
    for _ in range(tries):
        event = await trigger.wait_for_trigger()
        if event is not None:
            return event
    return None


class TestHub:
    async def test_ingest_dedups_marks_replies_and_reports_new_chats(self, make_hub):
        hub = make_hub()
        new = []
        hub.on_new_conversation(new.append)
        await hub.on_dispatch(EVENT_GROUP, group_event("M1", "G1", "P1", "a"))
        await hub.on_dispatch(EVENT_GROUP, group_event("M1", "G1", "P1", "a"))
        await hub.on_dispatch("READY", {})
        assert hub.store.head("group:G1") == 1
        assert new == ["group:G1"]
        sent = await hub.send("group:G1", "mine")
        assert sent.sent and sent.passive and sent.seq == 2
        out_id = hub.store.get("group:G1", 2)["msg_id"]
        await hub.on_dispatch(
            EVENT_GROUP,
            group_event(
                "M2", "G1", "P2", "re", message_reference={"message_id": out_id}
            ),
        )
        reply = hub.store.get("group:G1", 3)
        assert reply["to_self"] is True and reply["reply_seq"] == 2
        await hub.close()

    async def test_send_without_window_reports_passive_failure(
        self, make_hub, fake_api
    ):
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
        assert not hub.ledger.usable("group:G", "group")
        result = await hub.send("group:G", "late")
        assert not result.sent and result.reason == CLOSED_WINDOW
        assert fake_api.sent() == [], "no active message is attempted"
        await hub.close()


async def _names_done(hub):
    for job in list(hub._name_jobs.values()):
        await job


def _quote_event(msg_id, member, content, ref, author, **extra):
    return group_event(
        msg_id,
        "G1",
        member,
        content,
        message_type=103,
        message_scene={"ext": [f"ref_msg_idx={ref}"]},
        msg_elements=[
            {"author": author, "content": f"[消息内容] quoted {ref}\n[消息类型] 文本"}
        ],
        **extra,
    )


class TestHubChatDetails:
    async def test_platform_quotes_link_to_stored_messages_and_the_bot(self, make_hub):
        hub = make_hub()
        hub.remember_identity({"id": "BOT"})
        feed = await _feed(hub)
        first = group_event(
            "M1",
            "G1",
            "P1",
            "火锅吧",
            author={"member_openid": "P1", "username": "Kohaku"},
            message_scene={"ext": ["msg_idx=REFIDX_1"]},
        )
        await hub.on_dispatch(EVENT_GROUP, first)
        await hub.on_dispatch(
            EVENT_GROUP,
            _quote_event("M2", "P2", "好", "REFIDX_1", {"member_openid": "P1"}),
        )
        await hub.on_dispatch(
            EVENT_GROUP,
            _quote_event("M3", "P2", "那个", "REFIDX_OLD", {"member_openid": "P1"}),
        )
        await hub.on_dispatch(
            EVENT_GROUP,
            _quote_event("M4", "P3", "bot?", "REFIDX_B", {"member_openid": "BOT"}),
        )
        stored = [hub.store.get("group:G1", seq) for seq in (2, 3, 4)]
        assert stored[0]["reply_seq"] == 1 and not stored[0].get("to_self")
        assert "reply_seq" not in stored[1] and stored[1]["quote"]["alias"] == "Kohaku"
        assert stored[2]["to_self"] is True and stored[2]["quote"]["self"] is True
        text = (await _next(feed)).content
        assert '↩m1 Kohaku "火锅吧": 好' in text
        assert '↩Kohaku "quoted REFIDX_OLD": 那个' in text
        assert '↩you "quoted REFIDX_B": bot?' in text
        assert "(older message)" not in text
        await feed.stop()
        await hub.close()

    async def test_group_name_is_looked_up_once_and_failures_rest(
        self, make_hub, fake_api, monkeypatch, tmp_path
    ):
        hub = make_hub()
        fake_api.group_names["G1"] = "Cat Lovers"
        feed = await _feed(hub)

        def lookups(group):
            return [
                p for _, p, _ in fake_api.requests if p == f"/v2/groups/{group}/info"
            ]

        await hub.on_dispatch(EVENT_GROUP, group_event("A1", "G1", "P1", "hi"))
        await _names_done(hub)
        assert hub.store.conv_name("group:G1") == "Cat Lovers"
        event = await _next(feed)
        assert '<qq_update chat="g_g1" name="Cat Lovers" type="group">' in event.content
        await hub.on_dispatch(EVENT_GROUP, group_event("A2", "G1", "P1", "again"))
        await _names_done(hub)
        assert len(lookups("G1")) == 1, "known names are not fetched again"
        history = await QQHistoryTool().execute(
            {"chat": "g_g1"}, context=_context(tmp_path, feed)
        )
        assert '<qq_history chat="g_g1" name="Cat Lovers"' in history.output
        await hub.on_dispatch(EVENT_C2C, c2c_event("Z1", "U9", "dm"))
        await _next(feed)
        unsure = await QQSendTool().execute(
            {"message": "x"}, context=_context(tmp_path, feed)
        )
        assert "g_g1 (Cat Lovers)" in unsure.error and "u_u9" in unsure.error

        await hub.on_dispatch(EVENT_GROUP, group_event("B1", "G2", "P1", "x"))
        await hub.on_dispatch(EVENT_GROUP, group_event("B2", "G2", "P1", "y"))
        await _names_done(hub)
        await hub.on_dispatch(EVENT_GROUP, group_event("B3", "G2", "P1", "z"))
        await _names_done(hub)
        assert len(lookups("G2")) == 1, "a refused lookup is not retried at once"
        meta = hub.store.conv_meta("group:G2")
        assert meta["name_failed_at"] and hub.store.conv_name("group:G2") == ""
        event = await _next(feed)
        assert '<qq_update chat="g_g2" type="group">' in event.content

        day_ago = time.time() - 25 * 3600
        hub.store.update_conv("group:G2", name_checked_at=day_ago)
        fake_api.group_names["G2"] = "Dog Lovers"
        await hub.on_dispatch(EVENT_GROUP, group_event("B4", "G2", "P1", "w"))
        await _names_done(hub)
        assert (
            len(lookups("G2")) == 2 and hub.store.conv_name("group:G2") == "Dog Lovers"
        )
        assert hub.store.conv_meta("group:G2")["name_failed_at"] is None

        async def broken(openid):
            raise RuntimeError("boom")

        monkeypatch.setattr(hub.api, "group_info", broken)
        assert await hub.ingest(_record("group:G3", "C1")) == 1
        await _names_done(hub)
        assert hub.store.conv_meta("group:G3")["name_failed_at"]
        await hub.on_dispatch(EVENT_C2C, c2c_event("D1", "U1", "private"))
        await _names_done(hub)
        assert "name_checked_at" not in hub.store.conv_meta("c2c:U1")
        assert await hub.refresh_group_name("c2c:U1") == ""
        hub.unbound = True
        await hub.ingest(_record("group:G4", "E1"))
        assert "name_checked_at" not in hub.store.conv_meta("group:G4")
        await feed.stop()
        await hub.close()

    async def test_same_named_senders_are_told_apart(self, make_hub, fake_api):
        hub = make_hub()
        feed = await _feed(hub)
        for msg_id, member in (("A", "P1AA"), ("B", "P2BB")):
            await hub.on_dispatch(
                EVENT_GROUP,
                group_event(
                    msg_id,
                    "G1",
                    member,
                    f"hi from {member}",
                    author={"member_openid": member, "username": "Kohaku"},
                ),
            )
        text = (await _next(feed)).content
        assert "Kohaku#p1aa: hi from P1AA" in text
        assert "Kohaku#p2bb: hi from P2BB" in text
        result = await hub.send(
            "group:G1", "@Kohaku#p2bb yes", mention_format="markdown"
        )
        assert result.sent
        content = fake_api.sent()[-1]["markdown"]["content"]
        assert content == '<qqbot-at-user id="P2BB" /> yes'
        await feed.stop()
        await hub.close()


def _record(cid, msg_id):
    return {
        "conv": cid,
        "kind": cid.partition(":")[0],
        "msg_id": msg_id,
        "author": "P",
        "content": "x",
        "ts": time.time(),
        "self": False,
    }


class TestFeedTrigger:
    async def test_incremental_delivery_with_backfill_and_filters(self, make_hub):
        hub = make_hub()
        await hub.on_dispatch(EVENT_GROUP, group_event("H1", "G1", "P1", "old talk"))
        feed = await _feed(hub, conversations="groups", backfill=5, batch_delay=0.05)
        await hub.on_dispatch(EVENT_GROUP, group_event("N1", "G1", "P1", "new talk"))
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "private"))
        event = await _next(feed)
        text = event.content
        assert "<qq_backfill" in text and "old talk" in text
        assert "<qq_update" in text and "new talk" in text
        assert text.index("old talk") < text.index("<qq_update")
        assert "private" not in text, "c2c chats are outside conversations=groups"
        assert event.context["source"] == "qq" and event.context["chats"] == ["g_g1"]
        assert feed.known("group:G1") and not feed.known("c2c:U1")

        await hub.on_dispatch(EVENT_GROUP, group_event("N2", "G1", "P2", "second"))
        event = await _next(feed)
        assert "second" in event.content
        assert "new talk" not in event.content and "<qq_backfill" not in event.content
        await feed.stop()
        await hub.close()

    async def test_resume_keeps_cursor_and_hides_own_messages(self, make_hub, tmp_path):
        hub = make_hub()
        feed = await _feed(hub, subscriber="creature:a")
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "first"))
        assert "first" in (await _next(feed)).content
        await hub.send("c2c:U1", "my reply", by="creature:a")
        await hub.send("c2c:U1", "other creature", by="creature:b")
        await feed.stop()

        resumed = await _feed(hub, subscriber="creature:a")
        await hub.on_dispatch(EVENT_C2C, c2c_event("C2", "U1", "later"))
        event = await _next(resumed)
        assert "later" in event.content and "first" not in event.content
        assert "my reply" not in event.content
        assert "[you]: other creature" in event.content
        await resumed.stop()
        await hub.close()

    async def test_mention_mode_holds_chatter_until_addressed(self, make_hub):
        hub = make_hub()
        feed = await _feed(hub, wake="mention")
        await hub.on_dispatch(EVENT_GROUP, group_event("A", "G", "P", "chatter"))
        assert await _next(feed, tries=2) is None
        await hub.on_dispatch(EVENT_GROUP_AT, group_event("B", "G", "P", "hey bot"))
        event = await _next(feed)
        assert "chatter" in event.content and "[@you]: hey bot" in event.content
        assert event.context["mention"] is True
        await feed.stop()
        await hub.close()

    async def test_ambient_waits_for_quiet_then_cools_down(self, make_hub):
        hub = make_hub()
        feed = await _feed(
            hub, wake="ambient", quiet_period=0.2, min_wait=0.05, cooldown=30
        )
        await hub.on_dispatch(EVENT_GROUP, group_event("A", "G", "P", "one"))
        start = time.monotonic()
        event = await _next(feed, tries=20)
        assert event is not None and time.monotonic() - start >= 0.15
        await hub.on_dispatch(EVENT_GROUP, group_event("B", "G", "P", "two"))
        assert (
            await _next(feed, tries=3) is None
        ), "cooldown blocks a second ambient wake"
        await hub.on_dispatch(EVENT_GROUP_AT, group_event("C", "G", "P", "@bot"))
        event = await _next(feed)
        assert "two" in event.content and "@bot" in event.content
        await feed.stop()
        await hub.close()

    async def test_images_become_image_parts(self, make_hub, monkeypatch):
        hub = make_hub()
        feed = await _feed(hub)

        async def allow(url):
            return None

        monkeypatch.setattr(media, "check_public_url", allow)
        await feed._http.aclose()
        feed._http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, content=b"PNGDATA", headers={"content-type": "image/png"}
                )
            )
        )
        attach = [{"content_type": "image/png", "url": "https://img.test/a.png"}]
        await hub.on_dispatch(
            EVENT_C2C, c2c_event("I1", "U1", "look", attachments=attach)
        )
        event = await _next(feed)
        assert isinstance(event.content, list)
        assert event.content[1].url.startswith("data:image/png;base64,")
        assert event.context["image_count"] == 1
        await feed.stop()
        await hub.close()

    def test_resume_dict_and_post_setup(self):
        trigger = QQFeedTrigger(conversations=["g_1"], wake="mention")
        QQFeedTrigger.post_setup(trigger, SimpleNamespace(creature_id="qq:1:x"))
        again = QQFeedTrigger.from_resume_dict(trigger.to_resume_dict())
        assert again.subscriber == "creature:qq:1:x"
        assert again.conversations == ["g_1"] and again.policy.mode == "mention"


class TestTools:
    async def test_send_targets_hold_quote_and_history(
        self, make_hub, fake_api, tmp_path
    ):
        hub = make_hub()
        feed = await _feed(hub)
        ctx = _context(tmp_path, feed)
        send = QQSendTool()

        no_chat = await send.execute({"message": "hi"}, context=ctx)
        assert "no chat delivered yet" in no_chat.error

        await hub.on_dispatch(EVENT_GROUP, group_event("G-1", "G1", "P1", "hello"))
        await _next(feed)
        await hub.on_dispatch(
            EVENT_GROUP, group_event("X-1", "G9", "P1", "unseen chat")
        )
        refused = await send.execute({"message": "hi", "to": "g_g9"}, context=ctx)
        assert "only use chats your feed delivered" in refused.error

        await hub.on_dispatch(EVENT_GROUP, group_event("G-2", "G1", "P2", "news!"))
        held = await send.execute({"message": "hi"}, context=ctx)
        assert held.success and held.output.startswith("NOT SENT: new chat arrived")
        assert "news!" in held.output
        assert not fake_api.sent()

        sent = await send.execute({"message": "hi all", "reply_to": "m1"}, context=ctx)
        assert sent.output == "Sent as m3"
        body = fake_api.sent()[-1]
        assert body["content"] == "hi all" and body["msg_id"] == "G-2"
        assert body["msg_seq"] == 1
        assert body["message_reference"] == {"message_id": "G-1"}

        history = await QQHistoryTool().execute({"limit": 5}, context=ctx)
        assert "<qq_history" in history.output and "hello" in history.output
        assert "hi all" not in history.output, "history ends at the cursor"
        await feed.stop()
        await hub.close()

    async def test_send_image_and_errors(self, make_hub, fake_api, tmp_path):
        hub = make_hub()
        feed = await _feed(hub)
        ctx = _context(tmp_path, feed)
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "send a pic"))
        await _next(feed)
        (tmp_path / "cat.png").write_bytes(b"\x89PNG")
        send = QQSendTool()
        ok = await send.execute({"message": "", "media": "cat.png"}, context=ctx)
        assert ok.output.startswith("Sent as m")
        assert fake_api.sent("/files")[0]["file_data"]
        assert fake_api.sent()[-1]["msg_type"] == 7
        missing = await send.execute({"message": "x", "media": "nope.png"}, context=ctx)
        assert "media file not found" in missing.error
        insecure = await send.execute(
            {"message": "x", "media": "http://a/b.png"}, context=ctx
        )
        assert "https" in insecure.error
        bad_tag = await send.execute({"message": "x", "reply_to": "zz"}, context=ctx)
        assert "bad reply_to" in bad_tag.error
        fake_api.fail_send = "banned word"
        failed = await send.execute({"message": "x"}, context=ctx)
        assert failed.output.startswith("NOT SENT: QQ refused the message")
        assert "banned word" in failed.output and "Rephrase" in failed.output
        assert failed.exit_code == 1
        too_long = await send.execute({"message": "x" * 6001}, context=ctx)
        assert "longer than 3 chat messages" in too_long.error
        assert "Shorten it" in too_long.error
        await feed.stop()
        await hub.close()

    async def test_send_errors_name_the_next_step(self, make_hub, fake_api, tmp_path):
        hub = make_hub()
        send = QQSendTool(max_chars=10)
        lonely = _context(tmp_path)
        no_feed = await send.execute({"message": "hi"}, context=lonely)
        assert no_feed.error.endswith("Start one with qq_watch, then wait for chat.")
        feed = await _feed(hub)
        ctx = _context(tmp_path, feed)
        early = await send.execute({"message": "hi"}, context=ctx)
        assert early.error.endswith("Wait for chat to arrive, then name it with `to`.")
        await hub.on_dispatch(EVENT_GROUP, group_event("G-1", "G1", "P1", "hi"))
        await _next(feed)
        empty = await send.execute({"message": " "}, context=ctx)
        assert empty.error.endswith("Give message text, media, or both.")
        bad = await send.execute({"message": "x", "reply_to": "?"}, context=ctx)
        assert bad.error.endswith("Use a tag like m41, or leave reply_to out.")
        gone = await send.execute({"message": "x", "reply_to": "m50"}, context=ctx)
        assert gone.output == (
            "NOT SENT: m50 is not in this chat. Send again without reply_to."
        )
        unknown = await send.execute({"message": "x", "to": "g_zz"}, context=ctx)
        assert unknown.error.endswith(
            'Use the chat="…" handle from a <qq_update> block.'
        )
        no_att = await send.execute({"media": "m1.3"}, context=ctx)
        assert no_att.error.endswith("Use a tag the feed shows, such as [image m12.1].")
        lost = await send.execute({"media": "m99.1"}, context=ctx)
        assert lost.error.endswith("Ask the sender to post it again.")
        sticker = await send.execute({"media": "sticker:none.png"}, context=ctx)
        assert "Search with qq_sticker" in sticker.error
        (tmp_path / "r.pdf").write_bytes(b"pdf")
        group_file = await send.execute({"media": "r.pdf"}, context=ctx)
        assert group_file.error.endswith("or as an image or video.")
        split = await send.execute(
            {"message": "一二三四五。六七八九十。甲乙丙。"}, context=ctx
        )
        assert split.output == "Sent as m2, m3"
        assert [b["content"] for b in fake_api.sent()] == [
            "一二三四五。",
            "六七八九十。甲乙丙。",
        ]
        too_long = await send.execute({"message": "x" * 31}, context=ctx)
        assert "31 characters, longer than 3 chat messages of 10" in too_long.error
        assert len(fake_api.sent()) == 2
        await feed.stop()
        await hub.close()

    async def test_two_chats_need_explicit_target(self, make_hub, tmp_path):
        hub = make_hub()
        feed = await _feed(hub)
        ctx = _context(tmp_path, feed)
        await hub.on_dispatch(EVENT_C2C, c2c_event("C1", "U1", "a"))
        await hub.on_dispatch(EVENT_GROUP, group_event("G1", "G1", "P", "b"))
        event = await _next(feed)
        assert sorted(event.context["chats"]) == ["g_g1", "u_u1"]
        result = await QQSendTool().execute({"message": "x"}, context=ctx)
        assert "say which chat" in result.error and "u_u1" in result.error
        ok = await QQSendTool().execute({"message": "x", "to": "u_u1"}, context=ctx)
        assert ok.output.startswith("Sent as")
        assert feeds_of(ctx.agent) == [feed]
        assert feeds_of(None) == []
        await feed.stop()
        await hub.close()

    def test_descriptions_are_short_with_not_for(self):
        for tool in (QQSendTool(), QQHistoryTool()):
            assert len(tool.description) <= 160 and "Not for" in tool.description
        send = QQSendTool()
        exposed = [
            send.description,
            send.get_full_documentation(),
            str(send.get_parameters_schema()),
        ]
        assert not any(
            "sandbox" in text.lower() for text in exposed
        ), "the sandbox's own prompt introduces sandbox: handles"


class TestAuditRegressions:
    async def test_at_event_after_plain_duplicate_marks_mention(self, make_hub):
        hub = make_hub()
        await hub.on_dispatch(EVENT_GROUP, group_event("D1", "G", "P", "yo bot"))
        await hub.on_dispatch(EVENT_GROUP_AT, group_event("D1", "G", "P", "yo bot"))
        assert hub.store.head("group:G") == 1
        assert hub.store.get("group:G", 1)["mention"] is True
        await hub.close()

    def test_setup_schema_properties_are_typed(self):
        props = QQFeedTrigger.setup_param_schema["properties"]
        for name, spec in props.items():
            assert "type" in spec or "anyOf" in spec, name

    def test_unknown_options_are_rejected(self):
        with pytest.raises(TypeError, match="wakes"):
            QQFeedTrigger(wakes="all")


async def test_feed_without_credentials_fails_loudly():
    with pytest.raises(CredentialsMissing, match="kt-qqbot login"):
        await QQFeedTrigger().start()


async def test_reply_window_counts_from_the_earlier_of_send_and_receive(make_hub):
    hub = make_hub()
    skewed = group_event("S1", "G", "P", "old", timestamp=time.time() + 3600)
    await hub.on_dispatch(EVENT_GROUP, skewed)
    assert hub.ledger.usable("group:G", "group")
    hub.ledger._clock = lambda: time.time() + 360
    assert not hub.ledger.usable(
        "group:G", "group"
    ), "a future platform clock must not stretch the window"
    await hub.close()


async def test_qq_watch_installs_a_feed_keyed_by_creature(
    make_hub, monkeypatch, tmp_path
):
    from_store = make_hub()
    monkeypatch.setattr(hub_registry, "HUB_FACTORY", lambda creds: from_store)
    CredentialStore().save(Credentials(APP_ID, SECRET))
    ctx = _context(tmp_path)
    ctx.creature_id = "qq:me"
    result = await QQWatchTool().execute({"conversations": "groups"}, context=ctx)
    assert result.success, result.error
    feeds = feeds_of(ctx.agent)
    assert len(feeds) == 1 and feeds[0].subscriber == "creature:qq:me"
    assert feeds[0].conversations == "groups"
    await ctx.agent.trigger_manager.stop_all()
    assert hub_registry.running_hubs() == {}


async def test_reply_ledger_survives_hub_restart(make_hub, fake_api, tmp_path):
    path = tmp_path / "ledger.kvault"
    hub = make_hub(store_path=path)
    await hub.on_dispatch(EVENT_GROUP, group_event("FULL", "G", "P", "a"))
    for _ in range(5):
        assert (await hub.send("group:G", "x")).sent
    await hub.on_dispatch(EVENT_GROUP, group_event("PART", "G2", "P", "b"))
    assert (await hub.send("group:G2", "y")).passive
    await hub.on_dispatch(EVENT_GROUP, group_event("OLD", "G3", "P", "c"))
    hub.store.close()
    await hub.api.aclose()

    hub = make_hub(store_path=path)
    hub.ledger._clock = lambda: time.time() + 120
    before = len(fake_api.sent())
    full = await hub.send("group:G", "after restart")
    assert (
        not full.sent and full.reason == CLOSED_WINDOW
    ), "a spent msg_id is not reused"
    assert len(fake_api.sent()) == before
    part = await hub.send("group:G2", "again")
    assert part.sent
    body = fake_api.sent()[-1]
    assert body["msg_id"] == "PART" and body["msg_seq"] == 2, "msg_seq continues"
    hub.store.close()
    await hub.api.aclose()

    hub = make_hub(store_path=path)
    hub.ledger._clock = lambda: time.time() + 400
    hub.ledger.prune()
    assert not hub.ledger.usable("group:G3", "group")
    assert hub.store.slots() == {}, "expired slots are dropped from the vault"
    await hub.close()


class _BusyAgent:
    """Host agent stand-in: reports a running turn and records injections."""

    def __init__(self, busy=True):
        self.is_processing = busy
        self.injected = []

    async def inject_event(self, event):
        self.injected.append(event)
        return False


class TestMidTurn:
    async def test_chat_arriving_mid_turn_folds_into_the_running_turn(self, make_hub):
        hub = make_hub()
        agent = _BusyAgent()
        feed = QQFeedTrigger(batch_delay=0, wake="mention")
        feed.bind_host("qq-c1", "qq", agent)
        await feed.attach(hub)
        await feed.start()
        await hub.on_dispatch(EVENT_GROUP_AT, group_event("M3", "G", "P", "m3 hey bot"))
        assert "m3 hey bot" in (await _next(feed)).content
        await hub.on_dispatch(EVENT_GROUP, group_event("M4", "G", "P", "m4 also this"))
        for _ in range(50):
            if agent.injected:
                break
            await asyncio.sleep(0.02)
        assert agent.injected, "m4 must reach the running turn"
        assert "m4 also this" in agent.injected[0].content
        assert agent.injected[0].stackable
        assert await _next(feed, tries=2) is None, "m4 is not delivered twice"
        await feed.stop()
        await hub.close()

    async def test_idle_agent_gets_no_injection(self, make_hub):
        hub = make_hub()
        agent = _BusyAgent(busy=False)
        feed = QQFeedTrigger(batch_delay=0, wake="all")
        feed.bind_host("qq-c2", "qq", agent)
        await feed.attach(hub)
        await feed.start()
        await hub.on_dispatch(EVENT_GROUP, group_event("M1", "G", "P", "hello"))
        await asyncio.sleep(0.2)
        assert agent.injected == []
        assert "hello" in (await _next(feed)).content
        await feed.stop()
        await hub.close()


def test_feed_defaults_favor_low_latency():
    feed = QQFeedTrigger()
    policy = feed.policy
    assert (
        policy.quiet_period,
        policy.decay,
        policy.min_wait,
        policy.max_wait,
        policy.cooldown,
    ) == (10.0, 0.5, 2.0, 30.0, 15.0)
    assert feed.batch_delay == 0.5 and feed.image_options.timeout == 5.0
    assert feed.to_resume_dict()["image_timeout"] == 5.0
