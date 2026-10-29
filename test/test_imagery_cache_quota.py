"""``core.imagery.cache`` 与 ``core.imagery.quota`` 的离线测试。

缓存：键寻址、sidecar 半截状态视为未命中、latest、启动清理 ``.part``。
配额：**请求发出即计费、失败不返还**、跨天清零、留痕上限。
"""

from __future__ import annotations

import sys
import unittest
import base64
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402

from core import storage  # noqa: E402
from core.imagery import cache as cache_mod  # noqa: E402
from core.imagery.cache import ImageCache, key_for  # noqa: E402
from core.imagery.quota import MAX_EVENTS, DailyQuota, QuotaDataError  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGP8sEWDgYGBiQEMABaCAdDy4LuvAAAAAElFTkSuQmCC")
KEY1 = "a" * 32
KEY2 = "b" * 32
KEY3 = "c" * 32
KEY4 = "d" * 32
KEY5 = "e" * 32


class CacheTest(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.cache = ImageCache(self.root / "image-cache")

    def test_store_find_roundtrip(self) -> None:
        path = self.cache.store(KEY1, PNG, ".png", {"key": KEY1, "mime": "image/png", "created_at": "t1"})
        self.assertTrue(path.is_file())
        hit = self.cache.find(KEY1)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.read_bytes(), PNG)
        self.assertEqual(self.cache.read_meta(KEY1)["mime"], "image/png")

    def test_find_without_meta_is_miss(self) -> None:
        """半截写入（图在 sidecar 不在）按未命中处理，不回一张没有依据的图。"""
        self.cache.store(KEY2, PNG, ".png", {"key": KEY2})
        self.cache.meta_path(KEY2).unlink()
        self.assertIsNone(self.cache.find(KEY2))

    def test_store_rolls_back_orphan_image_when_meta_fails(self) -> None:
        original = cache_mod._atomic_write_bytes
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if calls["n"] == 2:  # 第一次是图，第二次是 sidecar
                raise OSError("disk full")
            return original(path, data)

        cache_mod._atomic_write_bytes = flaky
        try:
            with self.assertRaises(OSError):
                self.cache.store(KEY3, PNG, ".png", {"key": KEY3})
        finally:
            cache_mod._atomic_write_bytes = original
        self.assertIsNone(self.cache.find(KEY3), "sidecar 写失败时图要回滚，不留孤儿")

    def test_store_rejects_oversized(self) -> None:
        with self.assertRaises(ValueError):
            self.cache.store(KEY4, b"\x00" * (cache_mod.MAX_IMAGE_BYTES + 1), ".png", {"key": KEY4})

    def test_latest_picks_newest(self) -> None:
        self.cache.store(KEY1, PNG, ".png", {"key": KEY1, "created_at": "2026-10-25T10:00:00"})
        self.cache.store(KEY2, PNG, ".png", {"key": KEY2, "created_at": "2026-10-26T10:00:00"})
        hit = self.cache.latest()
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], KEY2)

    def test_latest_empty_dir(self) -> None:
        self.assertIsNone(self.cache.latest())

    def test_cleanup_tmp_removes_only_part_files(self) -> None:
        self.cache.store(KEY1, PNG, ".png", {"key": KEY1})
        part = self.cache.base / f".{KEY1}.png.part-abc123"
        actual_prefix_part = self.cache.base / f".{KEY1}.png.partabc123"
        unrelated = self.cache.base / "notes.part-backup"
        part.write_bytes(b"half")
        actual_prefix_part.write_bytes(b"half")
        unrelated.write_bytes(b"keep")
        # pathlib 的 glob 匹配点开头文件；直接断言清理前后
        self.assertEqual(self.cache.cleanup_tmp(), 2)
        self.assertFalse(part.exists())
        self.assertIsNotNone(self.cache.find(KEY1), "正式缓存不受清理影响")
        self.assertTrue(unrelated.exists(), "清理不碰非缓存文件")
        self.assertEqual(self.cache.cleanup_tmp(), 0)

    def test_prune_caps_images_and_preserves_latest(self) -> None:
        old_count, old_bytes = cache_mod.MAX_CACHE_IMAGES, cache_mod.MAX_CACHE_BYTES
        self.cache.base.mkdir(parents=True, exist_ok=True)
        unrelated = self.cache.base / "notes.txt"
        unrelated.write_text("keep", encoding="utf-8")
        try:
            cache_mod.MAX_CACHE_IMAGES = 2
            cache_mod.MAX_CACHE_BYTES = 1024 * 1024
            self.cache.store(KEY1, PNG, ".png", {"key": KEY1, "created_at": "2026-10-24T10:00:00"})
            self.cache.store(KEY2, PNG, ".png", {"key": KEY2, "created_at": "2026-10-25T10:00:00"})
            self.cache.store(KEY3, PNG, ".png", {"key": KEY3, "created_at": "2026-10-26T10:00:00"})
            self.assertIsNone(self.cache.find(KEY1))
            self.assertIsNotNone(self.cache.find(KEY2))
            self.assertIsNotNone(self.cache.find(KEY3))
            self.assertEqual(self.cache.latest()[0], KEY3)

            byte_cache = ImageCache(self.root / "byte-cache")
            byte_cache.store(KEY4, PNG, ".png", {"key": KEY4, "created_at": "2026-10-24T10:00:00"})
            pair_size = byte_cache.image_path(KEY4, ".png").stat().st_size + byte_cache.meta_path(KEY4).stat().st_size
            cache_mod.MAX_CACHE_IMAGES = 40
            cache_mod.MAX_CACHE_BYTES = pair_size * 2
            byte_cache.store(KEY1, PNG, ".png", {"key": KEY1, "created_at": "2026-10-25T10:00:00"})
            byte_cache.store(KEY5, PNG, ".png", {"key": KEY5, "created_at": "2026-10-26T10:00:00"})
            byte_files = list(byte_cache.base.iterdir())
            self.assertLessEqual(sum(path.stat().st_size for path in byte_files if path.is_file()), cache_mod.MAX_CACHE_BYTES)
            self.assertEqual(byte_cache.latest()[0], KEY5)
            self.assertTrue(unrelated.exists(), "裁剪不得碰缓存目录里的其他文件")
        finally:
            cache_mod.MAX_CACHE_IMAGES, cache_mod.MAX_CACHE_BYTES = old_count, old_bytes

    def test_cache_rejects_noncanonical_keys(self) -> None:
        with self.assertRaises(ValueError):
            self.cache.store("../outside", PNG, ".png", {"key": "../outside"})

    def test_key_for_deterministic_and_sensitive(self) -> None:
        payload = {"prompt": "a", "model": "m"}
        self.assertEqual(key_for(payload), key_for(dict(payload)))
        self.assertNotEqual(key_for(payload), key_for({**payload, "prompt": "b"}))
        self.assertNotEqual(key_for(payload), key_for({**payload, "model": "m2"}))


class QuotaTest(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.root / "imagery-quota.json"
        self.now = datetime(2026, 10, 26, 10, 30, tzinfo=TZ)
        self.quota = DailyQuota(self.path, storage.KeyedLocks())

    def test_fresh_state(self) -> None:
        state = self.quota.state(now=self.now)
        self.assertEqual(state["used"], 0)
        self.assertEqual(state["date"], "2026-10-26")

    def test_spend_accumulates_and_persists(self) -> None:
        import asyncio

        async def scenario():
            await self.quota.spend(result="sent", detail="a1111", now=self.now)
            await self.quota.spend(result="ok", detail="成功", now=self.now)

        asyncio.run(scenario())
        state = self.quota.state(now=self.now)
        self.assertEqual(state["used"], 2)
        # 新实例读同一份文件（落盘是权威，不是内存）
        self.assertEqual(DailyQuota(self.path).state(now=self.now)["used"], 2)
        self.assertEqual(state["events"][0]["result"], "ok", "最新事件在最前")

    def test_cross_day_resets(self) -> None:
        import asyncio

        async def scenario():
            await self.quota.spend(result="sent", detail="", now=self.now)
            await self.quota.spend(result="ok", detail="", now=self.now + timedelta(hours=2))

        asyncio.run(scenario())
        tomorrow = self.quota.state(now=self.now + timedelta(days=1))
        self.assertEqual(tomorrow["used"], 0)
        self.assertEqual(tomorrow["date"], "2026-10-27")

    def test_spend_raises_on_disk_failure(self) -> None:
        """记账落盘失败必须上抛——调用方据此拒绝发请求，费用上限不能被绕过。"""
        import asyncio

        read_only = self.root / "quota-dir"
        read_only.mkdir()
        locked = DailyQuota(read_only / "imagery-quota.json")
        original = storage.atomic_write_json

        def broken(path, payload):
            raise OSError("cannot write")

        storage.atomic_write_json = broken
        try:
            with self.assertRaises(OSError):
                asyncio.run(locked.spend(result="sent", detail="", now=self.now))
        finally:
            storage.atomic_write_json = original

    def test_corrupt_ledger_and_invalid_used_fail_closed(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        original = self.path.read_text(encoding="utf-8")
        self.assertIn("error", self.quota.state(now=self.now))
        with self.assertRaises(QuotaDataError):
            asyncio.run(self.quota.spend(result="sent", detail="", now=self.now))
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)

        self.path.write_bytes(b"\xff\xfe\xfa")
        original_bytes = self.path.read_bytes()
        self.assertIn("error", self.quota.state(now=self.now))
        with self.assertRaises(QuotaDataError):
            asyncio.run(self.quota.spend(result="sent", detail="", now=self.now))
        self.assertEqual(self.path.read_bytes(), original_bytes)

        for invalid in (True, -1, "1"):
            self.path.write_text(
                json.dumps({"date": "2026-10-26", "used": invalid, "events": []}), encoding="utf-8"
            )
            self.assertIn("error", self.quota.state(now=self.now), f"used={invalid!r} must fail closed")

    def test_events_capped(self) -> None:
        import asyncio

        async def scenario():
            for index in range(MAX_EVENTS + 5):
                await self.quota.spend(result=f"r{index}", detail="", now=self.now)

        asyncio.run(scenario())
        self.assertEqual(len(self.quota.state(now=self.now)["events"]), MAX_EVENTS)


if __name__ == "__main__":
    unittest.main()
