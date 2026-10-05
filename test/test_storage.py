"""``core.storage`` 的行为测试（第 0 步验收）。

覆盖：原子写 / 换行与编码契约 / mtime 缓存 / schema_version 策略 / 目录布局 / 进程内锁。
不需要 AstrBot，直接跑：

    $env:PYTHONDONTWRITEBYTECODE=1; py -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import storage  # noqa: E402
from support import TmpDirCase  # noqa: E402


class AtomicWriteTest(TmpDirCase):
    def test_creates_parent_dirs_and_writes_text(self) -> None:
        target = self.root / "a" / "b" / "note.txt"
        storage.atomic_write_text(target, "你好")
        self.assertEqual(target.read_text(encoding="utf-8"), "你好")

    def test_text_is_lf_only_no_crlf_translation(self) -> None:
        target = self.root / "diary.txt"
        storage.atomic_write_text(target, "第一行\n第二行\n")
        self.assertEqual(target.read_bytes(), "第一行\n第二行\n".encode("utf-8"))
        self.assertNotIn(b"\r", target.read_bytes())

    def test_read_write_roundtrip_keeps_raw_crlf(self) -> None:
        target = self.root / "raw.txt"
        target.write_bytes(b"a\r\nb\r\n")
        self.assertEqual(storage.read_text(target), "a\r\nb\r\n")

    def test_overwrite_leaves_no_temp_files(self) -> None:
        target = self.root / "diary.txt"
        storage.atomic_write_text(target, "v1\n")
        storage.atomic_write_text(target, "v2\n")
        storage.atomic_write_text(target, "v3\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "v3\n")
        leftovers = [p.name for p in self.root.iterdir() if p.name != "diary.txt"]
        self.assertEqual(leftovers, [])

    def test_read_text_default_when_missing(self) -> None:
        self.assertEqual(storage.read_text(self.root / "nope.txt", "缺省"), "缺省")

    def test_write_json_stamps_schema_version_and_keeps_chinese(self) -> None:
        target = self.root / "state.json"
        storage.atomic_write_json(target, {"mood": "开心"})
        raw = target.read_bytes()
        self.assertIn("开心".encode("utf-8"), raw)          # ensure_ascii=False
        self.assertNotIn(b"\\u5f00", raw)                    # 不是转义写法
        doc = json.loads(raw.decode("utf-8"))
        self.assertEqual(doc["schema_version"], storage.SCHEMA_VERSION)
        self.assertEqual(doc["mood"], "开心")
        self.assertTrue(raw.endswith(b"\n"))

    def test_write_json_does_not_mutate_caller_payload(self) -> None:
        payload = {"a": 1}
        storage.atomic_write_json(self.root / "x.json", payload)
        self.assertEqual(payload, {"a": 1})


class LoadJsonTest(TmpDirCase):
    def test_missing_or_broken_or_non_object_falls_back(self) -> None:
        self.assertEqual(storage.load_json(""), {})
        self.assertEqual(storage.load_json("", {"k": 1}), {"k": 1})
        self.assertEqual(storage.load_json("{oops"), {})
        self.assertEqual(storage.load_json("[1, 2]"), {})

    def test_default_is_copied_not_shared(self) -> None:
        first = storage.load_json("", {"a": 1})
        first["a"] = 2
        second = storage.load_json("", {"a": 1})
        self.assertEqual(second, {"a": 1})

    def test_missing_version_defaults_to_current(self) -> None:
        doc = storage.load_json('{"a": 1}')
        self.assertEqual(doc, {"a": 1})

    def test_older_version_is_migrated(self) -> None:
        migrations = {0: lambda d: {**d, "extra": True, "schema_version": 1}}
        doc = storage.load_json('{"schema_version": 0, "a": 1}', migrations=migrations)
        self.assertEqual(doc, {"schema_version": 1, "a": 1, "extra": True})

    def test_older_version_without_migration_is_returned_as_is(self) -> None:
        doc = storage.load_json('{"schema_version": 0, "a": 1}')
        self.assertEqual(doc, {"schema_version": 0, "a": 1})

    def test_newer_version_is_never_rewritten(self) -> None:
        newer = {"schema_version": storage.SCHEMA_VERSION + 98, "future": True}
        doc = storage.load_json(json.dumps(newer))
        self.assertEqual(doc, newer)

    def test_non_integer_version_is_tolerated(self) -> None:
        doc = storage.load_json('{"schema_version": "2", "a": 1}')
        self.assertEqual(doc, {"schema_version": "2", "a": 1})

    def test_read_json_from_disk(self) -> None:
        target = self.root / "notebook.json"
        storage.atomic_write_json(target, {"items": []})
        self.assertEqual(storage.read_json(target), {"schema_version": 1, "items": []})


class FileCacheTest(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.cache = storage.FileCache()
        self.target = self.root / "diary.txt"
        storage.atomic_write_text(self.target, "第一条\n")

    def test_second_read_does_not_touch_disk(self) -> None:
        with mock.patch.object(storage, "read_text", wraps=storage.read_text) as spy:
            self.assertEqual(self.cache.read_text(self.target), "第一条\n")
            self.assertEqual(self.cache.read_text(self.target), "第一条\n")
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(self.cache.hits, 1)
        self.assertEqual(self.cache.misses, 1)

    def test_external_change_with_same_size_is_noticed(self) -> None:
        self.assertEqual(self.cache.read_text(self.target), "第一条\n")
        old_mtime = os.stat(self.target).st_mtime_ns
        self.target.write_bytes("第二条\n".encode("utf-8"))   # 同样字节数
        os.utime(self.target, ns=(old_mtime + 1_000_000_000, old_mtime + 1_000_000_000))
        self.assertEqual(self.cache.read_text(self.target), "第二条\n")
        self.assertEqual(self.cache.hits, 0)

    def test_invalidate_forces_reread(self) -> None:
        self.cache.read_text(self.target)
        self.cache.invalidate(self.target)
        with mock.patch.object(storage, "read_text", wraps=storage.read_text) as spy:
            self.cache.read_text(self.target)
        self.assertEqual(spy.call_count, 1)

    def test_missing_file_returns_default_and_drops_entry(self) -> None:
        self.cache.read_text(self.target)
        os.unlink(self.target)
        self.assertEqual(self.cache.read_text(self.target, "空本子"), "空本子")

    def test_clear_drops_everything(self) -> None:
        self.cache.read_text(self.target)
        self.cache.clear()
        with mock.patch.object(storage, "read_text", wraps=storage.read_text) as spy:
            self.cache.read_text(self.target)
        self.assertEqual(spy.call_count, 1)

    def test_read_json_goes_through_same_cache(self) -> None:
        json_path = self.root / "names.json"
        storage.atomic_write_json(json_path, {"1": "甲"})
        with mock.patch.object(storage, "read_text", wraps=storage.read_text) as spy:
            self.cache.read_json(json_path)
            self.cache.read_json(json_path)
        self.assertEqual(spy.call_count, 1)


class LayoutTest(TmpDirCase):
    def test_paths_use_contract_file_names(self) -> None:
        layout = storage.Layout(self.root / "data")
        self.assertTrue(str(layout.diary).endswith(storage.DIARY_FILE))
        self.assertTrue(str(layout.love_diary).endswith(storage.LOVE_DIARY_FILE))
        self.assertTrue(str(layout.notebook).endswith(storage.NOTEBOOK_FILE))
        self.assertTrue(str(layout.state).endswith(storage.STATE_FILE))
        self.assertTrue(str(layout.affinity).endswith(storage.AFFINITY_FILE))
        self.assertTrue(str(layout.proactive).endswith(storage.PROACTIVE_FILE))
        self.assertTrue(str(layout.proactive_log).endswith(storage.PROACTIVE_LOG_FILE))
        self.assertTrue(str(layout.names).endswith(storage.NAMES_FILE))
        self.assertTrue(str(layout.state_history).endswith(storage.STATE_HISTORY_FILE))
        self.assertTrue(str(layout.diary_nudge).endswith(storage.DIARY_NUDGE_FILE))
        self.assertTrue(str(layout.trash_dir).endswith(storage.TRASH_DIR))
        self.assertEqual(len(layout.files()), 10)

    def test_all_files_live_directly_under_base_dir(self) -> None:
        layout = storage.Layout(self.root / "data")
        for path in layout.files():
            self.assertEqual(path.parent, layout.base_dir)

    def test_ensure_creates_base_and_trash(self) -> None:
        layout = storage.Layout(self.root / "deep" / "data").ensure()
        self.assertTrue(layout.base_dir.is_dir())
        self.assertTrue(layout.trash_dir.is_dir())

    def test_json_files_tuple_matches_layout(self) -> None:
        layout = storage.Layout(self.root)
        names = {path.name for path in layout.files()}
        for name in storage.JSON_FILES:
            self.assertIn(name, names)
        self.assertNotIn(storage.DIARY_FILE, storage.JSON_FILES)
        # 情绪历史与主动消息记录是 JSONL 追加文件，没有 schema_version 信封，不在 JSON_FILES 里
        self.assertNotIn(storage.STATE_HISTORY_FILE, storage.JSON_FILES)
        self.assertNotIn(storage.PROACTIVE_LOG_FILE, storage.JSON_FILES)


class KeyedLocksTest(unittest.TestCase):
    def test_same_key_serializes(self) -> None:
        async def scenario() -> None:
            locks = storage.KeyedLocks()
            first_in = asyncio.Event()
            release = asyncio.Event()
            second_in = asyncio.Event()

            async def first() -> None:
                async with locks.acquire("data.json"):
                    first_in.set()
                    await release.wait()

            async def second() -> None:
                await first_in.wait()
                async with locks.acquire("data.json"):
                    second_in.set()

            task_one = asyncio.create_task(first())
            task_two = asyncio.create_task(second())
            await first_in.wait()
            await asyncio.sleep(0.02)
            self.assertFalse(second_in.is_set(), "同一把锁不该被同时持有")
            release.set()
            await asyncio.gather(task_one, task_two)
            self.assertTrue(second_in.is_set())

        asyncio.run(scenario())

    def test_different_keys_do_not_block(self) -> None:
        async def scenario() -> None:
            locks = storage.KeyedLocks()
            held = asyncio.Event()
            release = asyncio.Event()

            async def holder() -> None:
                async with locks.acquire("a"):
                    held.set()
                    await release.wait()

            async def other() -> bool:
                await held.wait()
                async with locks.acquire("b"):
                    return True

            task = asyncio.create_task(holder())
            await held.wait()
            self.assertTrue(await asyncio.wait_for(other(), timeout=1))
            release.set()
            await task

        asyncio.run(scenario())

    def test_lock_released_after_exception(self) -> None:
        async def scenario() -> None:
            locks = storage.KeyedLocks()
            with self.assertRaises(RuntimeError):
                async with locks.acquire("x"):
                    raise RuntimeError("boom")
            async with locks.acquire("x"):
                pass

        asyncio.run(scenario())

    def test_lock_for_is_stable(self) -> None:
        locks = storage.KeyedLocks()
        self.assertIs(locks.lock_for("k"), locks.lock_for("k"))
        self.assertIsNot(locks.lock_for("k"), locks.lock_for("k2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
