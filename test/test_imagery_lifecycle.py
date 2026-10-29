"""Late transport completion must not publish a timed-out image or hold the slot forever."""

from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from test.test_imagery_api import FacadeCase, NOW, OverrideSettings, a1111_ok
from core.webui_portrait import PortraitError


class LateTransportLifecycleTest(FacadeCase):
    def test_missing_decoder_refuses_request_before_spend(self) -> None:
        self.seed_current_activity()
        imagery = self.build()
        with patch("core.imagery.api.ensure_validator", side_effect=PortraitError("缺少 Pillow")):
            result = asyncio.run(imagery.generate(now=NOW, trusted=True))
        self.assertFalse(result["ok"])
        self.assertIn("Pillow", result["error"])
        self.assertEqual(self.requests, [])
        self.assertEqual(imagery._quota.state(now=NOW)["used"], 0)

    def test_switching_server_does_not_reuse_another_servers_image(self) -> None:
        self.seed_current_activity()
        imagery = self.build()
        async def scenario():
            first = await imagery.generate(now=NOW, trusted=True)
            imagery.settings = OverrideSettings(self.settings, {**dict(self.settings.imagery), "base_url": "http://127.0.0.1:9876"})
            second = await imagery.generate(now=NOW, trusted=True)
            return first, second
        first, second = asyncio.run(scenario())
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(second["cached"])
        self.assertNotEqual(first["image"]["key"], second["image"]["key"])
        self.assertEqual(len(self.requests), 2)

    def test_timed_out_late_response_is_discarded_and_transport_slot_is_released(self) -> None:
        self.seed_current_activity()

        async def scenario():
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()
            self.addCleanup(release.set)

            def late_success(_request):
                started.set()
                try:
                    release.wait(timeout=5)
                    return a1111_ok()
                finally:
                    finished.set()

            self.respond = late_success
            settings = OverrideSettings(
                self.settings,
                {**dict(self.settings.imagery), "timeout_seconds": 0.05},
            )
            imagery = self.build(settings=settings)
            result = await imagery.generate(now=NOW, trusted=True)
            occupied_after_timeout = imagery._transport_active()
            no_image_published = imagery._cache.latest() is None
            transport_task = imagery._transport_task

            release.set()
            await asyncio.to_thread(finished.wait, 3)
            if transport_task is not None:
                await transport_task
            released_after_completion = not imagery._transport_active()
            used = imagery._quota.state(now=NOW)["used"]
            return result, occupied_after_timeout, no_image_published, released_after_completion, used

        result, occupied, no_image, released, used = asyncio.run(scenario())
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])
        self.assertTrue(occupied, "awaiter 超时后仍应占用传输槽，直到后台线程真实结束")
        self.assertTrue(no_image, "超时之后到达的成功响应不能再写缓存或发布图片")
        self.assertTrue(released, "后台线程结束后应释放传输槽")
        self.assertEqual(used, 1, "已发出的请求计费一次，晚到响应不重复记账")
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
