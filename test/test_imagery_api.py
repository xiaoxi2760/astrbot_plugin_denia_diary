"""``core.imagery.api``（门面）的离线集成测试：全链路走桩 transport，不碰网络。

覆盖任务 2 的验收矩阵：开关闸门、缓存命中与失效（活动 / 情绪 / 立绘）、每日上限、
超时不返还、并发点击、供应商异常、坏 MIME、过大内容、磁盘失败降级、取消收尾、
重启清理、**私密字符串不进请求与日志**。

⚠️ 每个测试**只开一个事件循环**（``drive``）：``KeyedLocks`` 的 ``asyncio.Lock``
在首次 await 时绑定循环，跨 ``asyncio.run`` 复用会 RuntimeError——那是测试搭法
错误，不是被测代码的缺陷（宿主进程只有一个循环）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase, private_session  # noqa: E402

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.diary.api import Diary  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402
from core.imagery import api as imagery_api_mod  # noqa: E402
from core.imagery.api import Imagery  # noqa: E402
from core.imagery.providers import HttpResponse  # noqa: E402
from core.life import Life  # noqa: E402
from core.state import State  # noqa: E402
from core.state.store import StateStore  # noqa: E402
from core.webui_portrait import PortraitStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 10, 30, tzinfo=TZ)
TODAY = "2026-10-26"
SECRET = "私密测试字符串-主人只对她说过的-绝不进图像请求"

PNG_PAYLOAD = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGP8sEWDgYGBiQEMABaCAdDy4LuvAAAAAElFTkSuQmCC")
PNG_B64 = base64.b64encode(PNG_PAYLOAD).decode("ascii")
PORTRAIT_A = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGPUaHrGwMDAxAAGABIsAZSdUquqAAAAAElFTkSuQmCC")
PORTRAIT_B = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGMMOFHBwMDAxAAGABLCAZQ2nVhKAAAAAElFTkSuQmCC")


def a1111_ok() -> HttpResponse:
    return HttpResponse(200, json.dumps({"images": [PNG_B64], "info": "{}"}).encode("utf-8"))


class OverrideSettings:
    """换掉 ``imagery`` 一组配置的轻包装（为了测 wait_for 的真实超时路径——
    ``load_settings`` 会把 timeout 夹到 ≥5 秒，测试等不起）。"""

    def __init__(self, base: object, imagery: dict) -> None:
        self._base = base
        self.imagery = imagery

    def __getattr__(self, name: str):  # noqa: D105 - 透传其余字段
        return getattr(self._base, name)


class FacadeCase(TmpDirCase):
    """全栈夹具：settings + layout + life + state + portraits + imagery（桩 transport）。"""

    def setUp(self) -> None:
        super().setUp()
        self.requests: list[object] = []
        self.respond = lambda req: a1111_ok()
        self.settings = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "imagery": self.imagery_config()}
        )
        self.layout = storage.Layout(self.root).ensure()
        self.locks = storage.KeyedLocks()
        self.life = Life(settings=self.settings, layout=self.layout, locks=self.locks)
        self.state = State(
            settings=self.settings,
            layout=self.layout,
            store=StateStore(layout=self.layout, locks=self.locks),
            locks=self.locks,
        )
        self.portraits = PortraitStore(self.layout, self.locks)

    # ---- 夹具 -------------------------------------------------------------------

    def imagery_config(self) -> dict:
        return {"enabled": True, "provider": "a1111", "base_url": "http://127.0.0.1:7860", "model": "test-checkpoint", "daily_limit": 3}

    def build(self, *, settings=None) -> Imagery:
        def transport(req):
            self.requests.append(req)
            return self.respond(req)

        return Imagery(
            settings=settings or self.settings,
            layout=self.layout,
            locks=self.locks,
            portraits=self.portraits,
            life=self.life,
            state=self.state,
            transport=transport,
        )

    def drive(self, scenario):
        """单循环执行：本文件所有 await 都必须发生在 ``asyncio.run`` 的这一次里。"""
        return asyncio.run(scenario())

    def seed_current_activity(self, title: str = "浇花", location: str = "阳台") -> None:
        day = {
            "date": TODAY,
            "timezone": "Asia/Shanghai",
            "activities": [
                {"id": "act_now", "start": "08:00", "end": "12:00", "title": title,
                 "location": location, "status": "planned", "outcome": ""},
            ],
            "generation": {"status": "ready", "error": "", "attempts": 1,
                           "next_retry_at": None, "lease_until": None, "token": None},
            "updated_at": None,
        }
        storage.atomic_write_json(
            self.layout.life,
            {"schema": 1, "revision": 1, "days": {f"{TODAY}|Asia/Shanghai": day}},
        )

    def request_bodies(self) -> list[dict]:
        return [json.loads(req.body.decode("utf-8")) for req in self.requests]


class GateTest(FacadeCase):
    def test_disabled_sends_nothing(self) -> None:
        self.seed_current_activity()
        settings = settings_mod.load_settings({"timezone": "Asia/Shanghai", "imagery": {"enabled": False}})

        async def scenario():
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertIn("关闭", result["error"])
        self.assertEqual(self.requests, [], "关着的时候一个请求都不许发")
        self.assertFalse(self.layout.imagery_quota.exists(), "没发请求就不该有记账")

    def test_untrusted_rejected(self) -> None:
        async def scenario():
            imagery = self.build()
            return await imagery.generate(now=NOW, trusted=False)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertEqual(self.requests, [])

    def test_missing_base_url_rejected_without_spend(self) -> None:
        settings = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "imagery": {"enabled": True, "provider": "a1111", "base_url": ""}}
        )

        async def scenario():
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.layout.imagery_quota.exists())


class SuccessAndCacheTest(FacadeCase):
    def test_success_flow(self) -> None:
        self.seed_current_activity()

        async def scenario():
            await self.state.observe(private_session(), word="有点开心", valence=1, arousal=1, now=NOW)
            imagery = self.build()
            result = await imagery.generate(now=NOW, trusted=True)
            metas = [json.loads(p.read_text(encoding="utf-8")) for p in self.layout.image_cache.glob("*.json")]
            return result, self.request_bodies(), metas

        result, bodies, metas = self.drive(scenario)
        self.assertTrue(result["ok"])
        self.assertFalse(result["cached"])
        self.assertTrue(result["image"]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(result["image"]["data_url"].split(",", 1)[1]), PNG_PAYLOAD)
        self.assertEqual(result["daily"]["used"], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("浇花", bodies[0]["prompt"], "当前活动进提示词")
        self.assertIn("有点开心", bodies[0]["prompt"], "可见期内的情绪词进提示词")
        self.assertTrue(any(self.layout.image_cache.glob("*.png")), "图落进 image-cache")
        self.assertTrue(any("浇花" in m.get("prompt", "") for m in metas), "sidecar 记录提示词")

    def test_cache_hit_skips_request_and_quota(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()
            first = await imagery.generate(now=NOW, trusted=True)
            second = await imagery.generate(now=NOW, trusted=True)
            return first, second

        first, second = self.drive(scenario)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(len(self.requests), 1, "命中缓存不再发请求")
        self.assertEqual(second["daily"]["used"], 1, "缓存命中不计数")

    def test_key_changes_with_activity(self) -> None:
        self.seed_current_activity(title="浇花")

        async def scenario():
            imagery = self.build()
            await imagery.generate(now=NOW, trusted=True)
            self.seed_current_activity(title="读书")
            return await imagery.generate(now=NOW, trusted=True)

        second = self.drive(scenario)
        self.assertFalse(second["cached"], "活动变了必须重新画")
        self.assertEqual(len(self.requests), 2)

    def test_key_changes_with_mood(self) -> None:
        self.seed_current_activity()

        async def scenario():
            await self.state.observe(private_session(), word="有点开心", valence=1, arousal=1, now=NOW)
            imagery = self.build()
            await imagery.generate(now=NOW, trusted=True)
            await self.state.observe(private_session(), word="有点低落", valence=-1, arousal=-1, now=NOW)
            return await imagery.generate(now=NOW, trusted=True)

        second = self.drive(scenario)
        self.assertFalse(second["cached"], "情绪变了必须重新画")

    def test_key_changes_with_portrait_reference(self) -> None:
        self.seed_current_activity()

        async def scenario():
            id_a = (await self.portraits.upload(PORTRAIT_A, "a.png", now=NOW))["id"]
            imagery = self.build()
            await imagery.generate(now=NOW, trusted=True)
            first_body = self.request_bodies()[0]
            id_b = (await self.portraits.upload(PORTRAIT_B, "b.png", now=NOW))["id"]
            await self.portraits.select(id_b)
            second = await imagery.generate(now=NOW, trusted=True)
            return first_body, second

        first_body, second = self.drive(scenario)
        self.assertIn("init_images", first_body, "a1111 + use_reference 时带参考立绘")
        self.assertFalse(second["cached"], "立绘换了，旧缓存必须失效")
        self.assertEqual(len(self.requests), 2)

    def test_reference_off_does_not_fingerprint_portrait(self) -> None:
        """参考图不参与生成时，立绘换了画面也不会变——不该浪费缓存。"""
        self.seed_current_activity()
        settings = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "imagery": {**self.imagery_config(), "use_reference": False}}
        )

        async def scenario():
            await self.portraits.upload(PORTRAIT_A, "a.png", now=NOW)
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            await imagery.generate(now=NOW, trusted=True)
            self.assertNotIn("init_images", self.request_bodies()[0])
            id_b = (await self.portraits.upload(PORTRAIT_B, "b.png", now=NOW))["id"]
            await self.portraits.select(id_b)
            return await imagery.generate(now=NOW, trusted=True)

        second = self.drive(scenario)
        self.assertTrue(second["cached"], "参考图不参与时，换立绘不影响画面")

    def test_openai_never_receives_reference_or_seed(self) -> None:
        self.seed_current_activity()
        settings = settings_mod.load_settings(
            {
                "timezone": "Asia/Shanghai",
                "imagery": {
                    "enabled": True, "provider": "openai",
                    "base_url": "https://api.openai.com/v1", "model": "gpt-image-1",
                    "api_key_env": "IMAGERY_TEST_KEY", "seed": 42, "steps": 30,
                },
            }
        )

        async def scenario():
            import os

            self.respond = lambda req: HttpResponse(
                200, json.dumps({"created": 1761000000, "data": [{"b64_json": PNG_B64}]}).encode("utf-8")
            )
            await self.portraits.upload(PORTRAIT_A, "a.png", now=NOW)
            await self.state.observe(private_session(), word="有点开心", valence=1, arousal=1, now=NOW)
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            os.environ["IMAGERY_TEST_KEY"] = "sk-test"
            try:
                return await imagery.generate(now=NOW, trusted=True)
            finally:
                del os.environ["IMAGERY_TEST_KEY"]

        result = self.drive(scenario)
        self.assertTrue(result["ok"], f"生成失败：{json.dumps({k: v for k, v in result.items() if k != 'image'}, ensure_ascii=False)}")
        body = self.request_bodies()[0]
        for absent in ("seed", "steps", "negative_prompt", "init_images"):
            self.assertNotIn(absent, body, f"openai 协议里不该有 {absent}")
        self.assertNotIn(
            base64.b64encode(PORTRAIT_A).decode("ascii")[:24],
            json.dumps(self.request_bodies(), ensure_ascii=False),
            "立绘字节绝不发给不支持参考图的提供方",
        )

    def test_openai_missing_key_errors_before_spend(self) -> None:
        settings = settings_mod.load_settings(
            {
                "timezone": "Asia/Shanghai",
                "imagery": {"enabled": True, "provider": "openai", "base_url": "https://api.openai.com/v1",
                            "model": "gpt-image-1", "api_key_env": "IMAGERY_MISSING_KEY"},
            }
        )

        async def scenario():
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertIn("api_key_env", result["error"])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.layout.imagery_quota.exists(), "请求没发出就不计费")

    def test_openai_cache_key_ignores_unsupported_parameters(self) -> None:
        imagery = self.build()
        scene = {"prompt": "test", "negative_prompt": "unsupported negative"}
        reference = {"bytes": None, "id": None}
        cfg = {"provider": "openai", "model": "gpt-image-1", "steps": 20, "seed": 17, "size": "1024x1024"}
        changed = {**cfg, "steps": 80, "seed": 0}
        self.assertEqual(
            imagery._key_payload(cfg, scene, reference),
            imagery._key_payload(changed, scene, reference),
            "OpenAI 不生效的 seed、steps、negative prompt 不应造成重复缓存键",
        )

    def test_a1111_without_model_never_reuses_cache(self) -> None:
        self.seed_current_activity()
        config = {**dict(self.settings.imagery), "model": ""}
        settings = OverrideSettings(self.settings, config)

        async def scenario():
            imagery = self.build(settings=settings)
            first = await imagery.generate(now=NOW, trusted=True)
            second = await imagery.generate(now=NOW, trusted=True)
            return first, second, imagery.status_payload(now=NOW)

        first, second, status = self.drive(scenario)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertFalse(first["cached"] or second["cached"])
        self.assertNotEqual(first["image"]["key"], second["image"]["key"])
        self.assertFalse(status["cache_reuse_supported"])
        self.assertEqual(len(self.requests), 2)

    def test_missing_image_validator_rejects_before_spend(self) -> None:
        self.seed_current_activity()
        original = imagery_api_mod.ensure_validator

        def missing():
            raise imagery_api_mod.PortraitError("缺少 Pillow")

        imagery_api_mod.ensure_validator = missing
        try:
            result = self.drive(lambda: self.build().generate(now=NOW, trusted=True))
        finally:
            imagery_api_mod.ensure_validator = original
        self.assertFalse(result["ok"])
        self.assertIn("Pillow", result["error"])
        self.assertEqual(self.requests, [])
        self.assertFalse(self.layout.imagery_quota.exists())


class QuotaAndFailureTest(FacadeCase):
    def test_corrupt_quota_is_visible_and_generation_fails_closed(self) -> None:
        self.seed_current_activity()
        self.layout.imagery_quota.write_text("{broken", encoding="utf-8")
        original = self.layout.imagery_quota.read_text(encoding="utf-8")

        async def scenario():
            imagery = self.build()
            return imagery.status_payload(now=NOW), await imagery.generate(now=NOW, trusted=True)

        status, result = self.drive(scenario)
        self.assertIn("error", status["daily"])
        self.assertIsNone(status["daily"]["used"])
        self.assertFalse(result["ok"])
        self.assertIn("账本损坏", result["error"])
        self.assertEqual(self.requests, [], "损坏账本时不能绕过上限发请求")
        self.assertEqual(self.layout.imagery_quota.read_text(encoding="utf-8"), original)

    def test_old_result_note_cannot_replace_new_day_ledger(self) -> None:
        imagery = self.build()
        tomorrow = datetime(2026, 10, 27, 0, 1, tzinfo=TZ)

        async def scenario():
            await imagery._quota.spend(result="sent", detail="tomorrow", now=tomorrow)
            await imagery._note("timeout", "old request", now=NOW)

        self.drive(scenario)
        import json as _json
        doc = _json.loads(self.layout.imagery_quota.read_text(encoding="utf-8"))
        self.assertEqual(doc["date"], "2026-10-27")
        self.assertEqual(doc["used"], 1)
        self.assertEqual(doc["events"][0]["detail"], "tomorrow")

    def test_daily_limit_blocks_before_request(self) -> None:
        self.seed_current_activity()
        settings = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "imagery": {**self.imagery_config(), "daily_limit": 1}}
        )

        async def scenario():
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            first = await imagery.generate(now=NOW, trusted=True)
            self.seed_current_activity(title="读书")  # 换提示词绕开缓存
            second = await imagery.generate(now=NOW, trusted=True)
            return first, second

        first, second = self.drive(scenario)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertIn("已用完", second["error"])
        self.assertEqual(len(self.requests), 1, "上限挡在发请求之前")

    def test_provider_error_keeps_spend(self) -> None:
        self.seed_current_activity()

        async def scenario():
            self.respond = lambda req: HttpResponse(500, json.dumps({"error": "server boom"}).encode("utf-8"))
            imagery = self.build()
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertIn("500", result["error"])
        self.assertEqual(result["daily"]["used"], 1, "请求已发出，已计费不返还")

    def test_timeout_keeps_spend_and_allows_next(self) -> None:
        self.seed_current_activity()

        async def scenario():
            def slow(req):
                import time
                try:
                    time.sleep(0.5)
                    return a1111_ok()
                finally:
                    finished.set()

            finished = threading.Event()
            self.respond = slow
            settings = OverrideSettings(self.settings, {**dict(self.settings.imagery), "timeout_seconds": 0.2})
            imagery = self.build(settings=settings)
            timed_out = await imagery.generate(now=NOW, trusted=True)
            self.seed_current_activity(title="读书")
            self.respond = lambda req: a1111_ok()  # 换回快的：证明配额没被锁死
            while_request_active = await imagery.generate(now=NOW, trusted=True)
            await asyncio.to_thread(finished.wait, 5)
            retried = await imagery.generate(now=NOW, trusted=True)
            return timed_out, while_request_active, retried

        timed_out, while_request_active, retried = self.drive(scenario)
        self.assertFalse(timed_out["ok"])
        self.assertIn("超时", timed_out["error"])
        self.assertEqual(timed_out["daily"]["used"], 1, "超时已计费，不返还")
        self.assertTrue(while_request_active.get("conflict"), "超时只结束等待，HTTP 工作线程仍占用并发槽")
        self.assertTrue(retried["ok"], "还在上限内，可以再试")
        self.assertEqual(len(self.requests), 2)

    def test_bad_mime_rejected(self) -> None:
        self.seed_current_activity()

        async def scenario():
            text_b64 = base64.b64encode(b"<html>not an image</html>").decode("ascii")
            self.respond = lambda req: HttpResponse(200, json.dumps({"images": [text_b64]}).encode("utf-8"))
            imagery = self.build()
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertIn("不可用", result["error"])
        self.assertEqual(result["daily"]["used"], 1, "坏图也是一次已发出的请求")

    def test_oversized_image_rejected(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()
            original = imagery_api_mod.MAX_IMAGE_BYTES
            imagery_api_mod.MAX_IMAGE_BYTES = 8
            try:
                return await imagery.generate(now=NOW, trusted=True)
            finally:
                imagery_api_mod.MAX_IMAGE_BYTES = original

        result = self.drive(scenario)
        self.assertFalse(result["ok"])
        self.assertIn("上限", result["error"])
        self.assertEqual(result["daily"]["used"], 1)

    def test_disk_failure_returns_data_url_not_wasted(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()

            def broken(*args, **kwargs):
                raise OSError("disk full")

            imagery._cache.store = broken
            return await imagery.generate(now=NOW, trusted=True)

        result = self.drive(scenario)
        self.assertTrue(result["ok"], "图已生成、钱已花：就地返还，不浪费")
        self.assertFalse(result["cached"])
        self.assertTrue(result["image"]["data_url"].startswith("data:image/png;base64,"))
        self.assertTrue(any("缓存写入失败" in n for n in result.get("notice", [])))
        self.assertEqual(result["daily"]["used"], 1)


class ConcurrencyAndLifecycleTest(FacadeCase):
    def test_concurrent_clicks_deduplicated(self) -> None:
        self.seed_current_activity()

        async def scenario():
            release = threading.Event()
            started = threading.Event()
            self.addCleanup(release.set)

            def blocking(req):
                started.set()
                release.wait(timeout=10)
                return a1111_ok()

            self.respond = blocking
            imagery = self.build()
            first = asyncio.create_task(imagery.generate(now=NOW, trusted=True))
            await asyncio.to_thread(started.wait, 10)
            second = await imagery.generate(now=NOW, trusted=True)
            release.set()
            return await first, second

        first, second = self.drive(scenario)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertTrue(second.get("conflict"), "并发点击要被明确拒绝，不是排队")
        self.assertTrue("在画" in second["error"] or "仍在运行" in second["error"])
        self.assertEqual(first["daily"]["used"], 1, "第二个点击没发请求，不计数")
        self.assertEqual(len(self.requests), 1)

    def test_cancel_releases_lock_and_leaves_no_part_files(self) -> None:
        self.seed_current_activity()

        async def scenario():
            release = threading.Event()
            started = threading.Event()
            finished = threading.Event()
            self.addCleanup(release.set)

            def blocking(req):
                started.set()
                try:
                    release.wait(timeout=10)
                    return a1111_ok()
                finally:
                    finished.set()

            self.respond = blocking
            imagery = self.build()
            task = asyncio.create_task(imagery.generate(now=NOW, trusted=True))
            await asyncio.to_thread(started.wait, 5)
            task.cancel()
            cancelled = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled = True
            during_transport = await imagery.generate(now=NOW, trusted=True)
            release.set()
            await asyncio.to_thread(finished.wait, 5)
            second = await imagery.generate(now=NOW, trusted=True)
            parts = [p for p in self.layout.image_cache.iterdir() if ".part" in p.name]
            return cancelled, during_transport, second, parts

        cancelled, during_transport, second, parts = self.drive(scenario)
        self.assertTrue(cancelled, "取消必须穿透，不能被吞成假成功")
        self.assertTrue(during_transport.get("conflict"), "取消的awaiter结束时，真实HTTP线程仍占用槽位")
        self.assertTrue(second["ok"], "取消后锁已释放，可以再画")
        self.assertEqual(parts, [], "不留 .part 残骸")

    def test_restart_cleans_part_files(self) -> None:
        self.layout.image_cache.mkdir(parents=True, exist_ok=True)
        temp_key = "a" * 32
        stale = self.layout.image_cache / f".{temp_key}.png.part-xyz"
        stale.write_bytes(b"half-written")
        self.build()  # 新实例 = 模拟重启
        self.assertFalse(stale.exists(), "启动时清掉上次死在半路的临时文件")

    def test_lock_released_after_success(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()
            result = await imagery.generate(now=NOW, trusted=True)
            return result, imagery._busy.locked()

        result, still_locked = self.drive(scenario)
        self.assertTrue(result["ok"])
        self.assertFalse(still_locked, "成功路径也要释放并发锁")


class PrivacyTest(FacadeCase):
    def test_private_diary_never_reaches_request_or_disk(self) -> None:
        """日记里有的私密串：请求体、缓存、记账、响应里一律不出现。

        门面的数据流只接 Life / State 的公开层——这条测试把"确实没接"钉死。
        """
        self.seed_current_activity()

        async def scenario():
            diary = Diary(
                settings=self.settings,
                layout=self.layout,
                store=DiaryStore(layout=self.layout, locks=self.locks, name_lookup=lambda who: ""),
                locks=self.locks,
            )
            await diary.write_async(
                private_session(),
                text=f"今天聊到了{SECRET}，只有我们知道。",
                mood="窃喜",
                now=NOW,
            )
            await self.state.observe(private_session(), word="有点开心", valence=1, arousal=1, now=NOW)
            imagery = self.build()
            result = await imagery.generate(now=NOW, trusted=True)
            disk_texts = [p.read_text(encoding="utf-8", errors="replace") for p in self.layout.image_cache.iterdir()]
            disk_texts.append(
                self.layout.imagery_quota.read_text(encoding="utf-8") if self.layout.imagery_quota.exists() else ""
            )
            return result, self.request_bodies(), disk_texts

        result, bodies, disk_texts = self.drive(scenario)
        self.assertTrue(result["ok"])
        for body in bodies:
            self.assertNotIn(SECRET, json.dumps(body, ensure_ascii=False))
        for text in disk_texts:
            self.assertNotIn(SECRET, text)
            self.assertNotIn("窃喜", text, "私聊日记里的情绪词也不落盘")
        self.assertNotIn(SECRET, json.dumps(result, ensure_ascii=False))


class StatusPayloadTest(FacadeCase):
    def test_status_shape_and_no_side_effects(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()
            return imagery.status_payload(now=NOW)

        payload = self.drive(scenario)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["provider"], "a1111")
        self.assertEqual(payload["daily"], {"date": TODAY, "used": 0, "limit": 3})
        self.assertTrue(payload["capabilities"]["known"])
        self.assertTrue(payload["capabilities"]["reference_image"])
        self.assertIsNone(payload["latest"])
        self.assertEqual(self.requests, [], "status 是纯只读")

    def test_status_openai_reference_notice(self) -> None:
        settings = settings_mod.load_settings(
            {
                "timezone": "Asia/Shanghai",
                "imagery": {"enabled": True, "provider": "openai", "base_url": "https://api.openai.com/v1",
                            "model": "gpt-image-1", "use_reference": True},
            }
        )

        async def scenario():
            imagery = self.build(settings=OverrideSettings(self.settings, dict(settings.imagery)))
            return imagery.status_payload(now=NOW)

        payload = self.drive(scenario)
        self.assertFalse(payload["capabilities"]["reference_image"])
        self.assertIn("不支持参考图", payload.get("notice", ""))

    def test_image_payload_empty_then_full(self) -> None:
        self.seed_current_activity()

        async def scenario():
            imagery = self.build()
            empty = imagery.image_payload()
            generated = await imagery.generate(now=NOW, trusted=True)
            return empty, generated, imagery.image_payload()

        empty, generated, full = self.drive(scenario)
        self.assertFalse(empty["ok"])
        self.assertTrue(generated["ok"])
        self.assertTrue(full["ok"])
        self.assertTrue(full["image"]["data_url"].startswith("data:image/png;base64,"))
        self.assertIn("浇花", full["image"]["prompt"])


if __name__ == "__main__":
    unittest.main()
