"""群社交 WebUI handler 的薄壳测试（群社交任务）。

判定都在 core.social（见 ``test_social.py``）；这里只验 handler 这层接线：
参数解析、200 + ``{ok, error}`` 语义、400 形状错误，以及 preview **绝不发送**。
手法照 ``test_webui_portrait``：假 request + 真门面。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import TmpDirCase  # noqa: E402

from core import storage  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core.social import Social  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 14, 0, tzinfo=TZ)
GROUP = "aiocqhttp:GroupMessage:555"


class SocialHandlerTest(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers

        self.handlers = handlers
        raw = {
            "timezone": "Asia/Shanghai",
            "scope": {"mode": "all"},
            "social": {"self_names": "小澄"},
        }
        resolved = settings_mod.load_settings(raw)
        layout = storage.Layout(self.root).ensure()
        self.social = Social(settings=resolved, layout=layout, locks=storage.KeyedLocks())
        self.deps = SimpleNamespace(social=self.social, _now=lambda: NOW)

    def call(self, name: str, *, body=None, query=None):
        fake = SimpleNamespace(
            query=query or {},
            username="admin",
            content_length=None,
            json=lambda default=None: _async_value(body if body is not None else default),
            files=None,
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)[name]
            return asyncio.run(self.handlers.logged_handler(name, handler)())
        finally:
            self.handlers.request = original

    def data_of(self, response):
        self.assertEqual(response["_stub"], "json_response")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def error_of(self, response):
        self.assertEqual(response["_stub"], "error_response")
        self.assertEqual(response["status"], 400)
        return response["message"]

    async def _seed_group(self) -> None:
        await self.social.save_group(
            {"platform": "aiocqhttp", "group_id": "555", "name": "测试群"}, now=NOW
        )

    def test_get_returns_payload(self) -> None:
        data = self.data_of(self.call("social_get"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["scope_mode"], "all")
        self.assertEqual(data["self_names"], ["小澄"])

    def test_group_save_delete_roundtrip(self) -> None:
        result = self.data_of(self.call("social_group", body={
            "action": "save", "platform": "aiocqhttp", "group_id": "555", "name": "测试群",
        }))
        self.assertTrue(result["ok"])
        self.assertEqual(self.social.store.read()["groups"][GROUP]["enabled"], False)

        result = self.data_of(self.call("social_group", body={
            "action": "save", "umo": GROUP, "enabled": True,
        }))
        self.assertTrue(result["ok"])
        self.assertTrue(self.social.store.read()["groups"][GROUP]["enabled"])

        result = self.data_of(self.call("social_group", body={"action": "delete", "umo": GROUP}))
        self.assertTrue(result["ok"])
        self.assertEqual(self.social.store.read().get("groups"), {})

    def test_group_bad_action_is_400(self) -> None:
        self.assertIn("action", self.error_of(self.call("social_group", body={"action": "nope"})))
        self.assertIn("umo", self.error_of(self.call("social_group", body={"action": "delete"})))

    def test_participant_requires_registered_group(self) -> None:
        result = self.data_of(self.call("social_participant", body={
            "action": "save", "group": GROUP, "id": "bot1", "is_bot": True,
        }))
        self.assertFalse(result["ok"])
        self.assertIn("先登记群", result["error"])
        asyncio.run(self._seed_group())
        result = self.data_of(self.call("social_participant", body={
            "action": "save", "group": GROUP, "id": "bot1",
            "name": "小助手", "is_bot": True, "allow_interaction": True,
        }))
        self.assertTrue(result["ok"])
        participant = self.social.store.read()["participants"][GROUP]["bot1"]
        self.assertTrue(participant["is_bot"])

    def test_preview_topic_without_material_says_so(self) -> None:
        asyncio.run(self._seed_group())
        data = self.data_of(self.call("social_preview", body={"kind": "topic", "group": GROUP}))
        self.assertFalse(data["ok"])  # 没有话题素材：说清楚，不假装有候选

    def test_preview_unknown_kind_is_400(self) -> None:
        self.assertIn("kind", self.error_of(self.call("social_preview", body={"kind": "?", "group": GROUP})))


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
