"""Cross-layer regressions for the six companion improvements."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from support import NOW, private_session
from test_main import PluginCase, _FakeEvent, _FakeCronManager, _parts_text, _sent_result


class UpdatesIntegrationTest(PluginCase):
    async def pending_comment(self, plugin):
        await plugin.diary.store.append("normal", date="2026-10-04", time="21:30", mood="开心", tag="和某某", body="今天很开心", person_id="10001")
        entry = plugin.diary.store.read_entries("normal")[0]
        sid = self.module.fmt.entry_seg_id(entry)
        draft = await plugin.comments.save_draft("normal", sid, "明天我陪你聊聊。", NOW)
        await plugin.comments.publish(draft["entry"]["id"], NOW)

    def test_comment_reaches_only_next_private_request_within_budget(self):
        async def scenario():
            plugin = self.make_plugin()
            plugin._now = lambda: NOW
            await self.pending_comment(plugin)
            request_type = sys.modules["astrbot.api.provider"].ProviderRequest
            first, second = request_type(), request_type()
            await plugin.inject_diary_hint(_FakeEvent(message_str="明天聊什么"), first)
            await plugin.inject_diary_hint(_FakeEvent(), second)
            return _parts_text(first), _parts_text(second)
        first, second = asyncio.run(scenario())
        self.assertIn("明天我陪你聊聊", first)
        self.assertNotIn("明天我陪你聊聊", second)
        self.assertLessEqual(len(first), 400)

    def test_comment_can_be_delivered_in_next_active_payload(self):
        async def scenario():
            manager = _FakeCronManager()
            plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
            plugin._now = lambda: NOW
            await self.pending_comment(plugin)
            await plugin._wake("aiocqhttp:FriendMessage:10001", "原本的主动素材")
            await plugin._wake("aiocqhttp:FriendMessage:10001", "下一次")
            return manager.active
        jobs = asyncio.run(scenario())
        self.assertIn("明天我陪你聊聊", jobs[0]["payload"]["note"])
        self.assertNotIn("明天我陪你聊聊", jobs[1]["payload"]["note"])

    def test_actual_other_target_success_cannot_confirm_original_session(self):
        async def scenario():
            plugin = self.make_plugin()
            event = _FakeEvent(extras={"cron_job": {"id": "test"}})
            tool = SimpleNamespace(name="send_message_to_user")
            await plugin.confirm_proactive_sent(event, tool, {"session": "aiocqhttp:FriendMessage:20002"}, _sent_result("Message sent to session aiocqhttp:FriendMessage:20002"))
            return plugin.proactive.store.read(), plugin.proactive.store.read_log_lines()
        doc, log = asyncio.run(scenario())
        self.assertEqual(doc.get("sessions", {}), {})
        self.assertEqual(log, [])

    def test_failed_active_dispatch_returns_comment_to_pending(self):
        async def scenario():
            manager = _FakeCronManager()
            manager.add_active_job = AsyncMock(side_effect=RuntimeError("scheduler down"))
            plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
            plugin._now = lambda: NOW
            await self.pending_comment(plugin)
            await plugin._wake("aiocqhttp:FriendMessage:10001", "素材")
            record = plugin.comments._read()["comments"][0]
            self.assertEqual(record["status"], "pending")
            self.assertEqual(plugin.proactive.store.read()["diagnostic"]["status"], "failed")
            self.assertIn("明天我陪你聊聊", await plugin._comment_context(self.module.Session.from_umo("aiocqhttp:FriendMessage:10001")))
        asyncio.run(scenario())

    def test_broken_diagnostic_does_not_prevent_active_dispatch(self):
        async def scenario():
            plugin = self.make_plugin()
            plugin._now = lambda: NOW
            decision = {"kind": "wake", "umo": "aiocqhttp:FriendMessage:10001", "slot": "promise", "note": "素材"}
            plugin.proactive.patrol = AsyncMock(return_value=decision)
            plugin._wake = AsyncMock()
            with patch.object(self.module.diagnostics, "check_snapshot", side_effect=RuntimeError("bad contacts")):
                await plugin._proactive_patrol()
            plugin._wake.assert_awaited_once_with(decision["umo"], "素材")
        asyncio.run(scenario())

    def test_localonly_policy_is_forwarded_to_shared_card_renderer(self):
        plugin = self.make_plugin()
        plugin.html_render = AsyncMock()
        renderer = AsyncMock(return_value=None)
        with patch.object(self.module.card_render, "render_html", renderer):
            asyncio.run(plugin.render_card_html("html", {}, "key"))
        self.assertIs(renderer.call_args.kwargs["allow_remote"], False)

    def test_localonly_plain_and_pretty_fallback_never_call_host_t2i(self):
        async def scenario(render):
            plugin = self.make_plugin({"diary": {"card_render": render}})
            plugin._now = lambda: NOW
            await plugin.diary.write_async(private_session(), text="私密的日记", now=NOW)
            remote, host_t2i = AsyncMock(), AsyncMock()
            plugin.html_render, plugin.text_to_image = remote, host_t2i
            event = _FakeEvent()
            event.plain_result = lambda value: ("text", value)
            event.image_result = lambda value: ("image", value)
            with patch.object(plugin, "_card_png", AsyncMock(return_value=None)), patch.object(self.module.card_render, "render_pil", AsyncMock(return_value=None)):
                results = [value async for value in plugin.diary_card(event, "10-04")]
            self.assertEqual(results[0][0], "text")
            remote.assert_not_awaited()
            host_t2i.assert_not_awaited()
        for render in ("plain", "pretty"):
            asyncio.run(scenario(render))
