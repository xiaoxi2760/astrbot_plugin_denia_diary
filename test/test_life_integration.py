"""日程与真实适配器的离线验收；模型、事件与时钟替身，不联系外部服务。"""
import asyncio
import json
from types import SimpleNamespace

from support import NOW, private_session, group_session
from test_main import PluginCase, _FakeEvent, _parts_text
from core import compose, settings, webui_settings


def response():
    return json.dumps({"date": NOW.date().isoformat(), "activities": [
        {"start": "09:00", "end": "10:00", "title": "读完短篇", "location": "书桌"},
        {"start": "21:00", "end": "22:00", "title": "整理读书笔记", "location": "窗边"},
        {"start": "22:00", "end": "23:00", "title": "准备休息", "location": "家里"},
    ]}, ensure_ascii=False)


class LifeIntegrationTest(PluginCase):
    def fixture(self, **config):
        calls = []

        async def text_chat(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(completion_text=response())

        provider = SimpleNamespace(text_chat=text_chat)

        async def get_provider():
            return provider

        async def persona():
            return {"prompt": "喜欢看书和安静散步的角色"}

        context = SimpleNamespace(get_using_provider_async=get_provider,
                                  persona_manager=SimpleNamespace(get_default_persona_v3=persona))
        plugin = self.make_plugin({"love_peers": ["10001"], **config}, context=context)
        plugin._now = lambda: NOW
        return plugin, calls, provider

    def test_background_does_not_depend_on_proactive_and_unloads_cleanly(self):
        async def run():
            plugin, calls, _ = self.fixture(subsystems={"proactive": False})
            await plugin.initialize()
            try:
                for _ in range(100):
                    if plugin.life_snapshot().get("activities"):
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(plugin.life_snapshot()["activities"])
                self.assertEqual(len(calls), 1)
                for _ in range(3):
                    plugin.life_snapshot()  # 页面打开/刷新完全只读
                self.assertEqual(len(calls), 1)
                task = plugin._life_task
            finally:
                await plugin.terminate()
            self.assertTrue(task.done())
            self.assertIsNone(plugin._life_task)
        asyncio.run(run())

    def test_planning_only_sends_public_inputs_and_preserves_diary_recall(self):
        async def run():
            plugin, calls, _ = self.fixture(scope={"mode": "all"})
            await plugin.diary.write_async(private_session(), text="只有恋爱本知道的紫色密码", now=NOW)
            result = await plugin.life.tick(plugin._life_generate, now=NOW)
            self.assertTrue(result["ok"], result)
            self.assertNotIn("紫色密码", json.dumps(calls, ensure_ascii=False))
            self.assertIn("喜欢看书", calls[0]["prompt"])
            self.assertEqual(calls[0]["contexts"], [])
            private = compose.compose_prompt(plugin.diary, private_session(), state=plugin.state, life=plugin.life, now=NOW)
            public = compose.compose_prompt(plugin.diary, group_session(), state=plugin.state, life=plugin.life, now=NOW)
            self.assertIn("整理读书笔记", private)
            self.assertIn("紫色密码", private)
            self.assertIn("整理读书笔记", public)
            self.assertNotIn("紫色密码", public)
            self.assertLessEqual(len(private), 400)
            self.assertLessEqual(len(public), 400)
            req = self.module.ProviderRequest()
            await plugin.inject_diary_hint(_FakeEvent(message_str="今天在做什么"), req)
            self.assertIn("整理读书笔记", _parts_text(req))
            self.assertEqual(req.system_prompt, "")
            self.assertLessEqual(len(_parts_text(req)), 400)
            from core.proactive.triggers import Candidate
            candidate = Candidate(slot="promise", umo=private_session().umo, session=private_session(),
                                  fragment="聊聊今天", instruction="接着聊", priority=0)
            self.assertIn("整理读书笔记", plugin.proactive._build_note(candidate, now=NOW))
        asyncio.run(run())

    def test_stale_date_and_revision_cannot_replace_today(self):
        async def run():
            plugin, _, _ = self.fixture()
            await plugin.life.tick(plugin._life_generate, now=NOW)
            snapshot = plugin.life_snapshot()
            body = {"date": "2000-01-01", "timezone": snapshot["timezone"],
                    "expected_revision": snapshot["revision"], "activities": []}
            result = await plugin.life_action("replace", body, trusted=True)
            self.assertFalse(result["ok"])
            self.assertTrue(result["conflict"])
            self.assertEqual(plugin.life_snapshot()["activities"], snapshot["activities"])
            body.update(date=snapshot["date"], expected_revision=-999)
            self.assertTrue((await plugin.life_action("replace", body, trusted=True))["conflict"])
        asyncio.run(run())

    def test_disabled_runtime_never_calls_provider(self):
        async def run():
            plugin, calls, _ = self.fixture(enabled=False)
            await plugin.initialize()
            self.assertIsNone(plugin._life_task)
            self.assertIn("关闭", await plugin.life_today(_FakeEvent()))
            self.assertFalse((await plugin.life.tick(plugin._life_generate, now=NOW))["ok"])
            self.assertEqual(calls, [])
            await plugin.terminate()
        asyncio.run(run())

    def test_provider_contract_supports_sync_old_host_and_explicit_id(self):
        async def run():
            plugin, calls, provider = self.fixture()
            plugin.context = SimpleNamespace(get_using_provider=lambda: provider)
            self.assertEqual(await plugin._life_generate("plan"), response())
            plugin.settings = settings.load_settings({"life": {"provider_id": "chosen"}})
            ids = []
            plugin.context = SimpleNamespace(get_provider_by_id=lambda key: ids.append(key) or provider)
            self.assertEqual(await plugin._life_generate("plan"), response())
            self.assertEqual(ids, ["chosen"])
            self.assertEqual(len(calls), 2)
        asyncio.run(run())

    def test_settings_schema_alignment_and_limits(self):
        self.assertEqual(webui_settings.verify_schema_alignment(webui_settings.load_schema_file()), [])
        normalized = settings.load_settings({"life": {"max_attempts": 500}})
        self.assertEqual(normalized.life["max_attempts"], 5)
        self.assertTrue(normalized.warnings)
