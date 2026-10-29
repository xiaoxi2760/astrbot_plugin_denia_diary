"""``core/voice/artifacts``（音频台账）的行为测试：引用保底 / 保留期 / 超容补刀 / 只删自己记的。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path as _P
from zoneinfo import ZoneInfo

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from support import TmpDirCase  # noqa: E402

from core.voice import artifacts  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)


def ts(minutes: float = 0.0, hours: float = 0.0) -> str:
    return (NOW - timedelta(minutes=minutes, hours=hours)).isoformat(timespec="seconds")


class PlanCleanupTest(unittest.TestCase):
    def test_old_entries_selected_newest_kept(self) -> None:
        entries = {
            "/t/old1.wav": ts(hours=8),
            "/t/old2.wav": ts(hours=7),
            "/t/fresh.wav": ts(minutes=5),
        }
        stale = artifacts.plan_cleanup(entries, NOW, retention_hours=6)
        self.assertEqual(stale, ["/t/old1.wav", "/t/old2.wav"], "fresh 在保留期内，绝不删")

    def test_min_keep_guard_never_deletes_young_files(self) -> None:
        """红线：10 分钟保底期内的文件即使超容 / Retention 也不删（发送前被删=白合成）。"""
        entries = {f"/t/{i}.wav": ts(minutes=3) for i in range(50)}
        stale = artifacts.plan_cleanup(entries, NOW, retention_hours=1, max_entries=20)
        self.assertEqual(stale, [], "全是保底期内的：一个不删")

    def test_over_capacity_prunes_oldest_outside_keep_window(self) -> None:
        entries = {f"/t/{i}.wav": ts(minutes=20 + i) for i in range(30)}  # 20~49 分钟前
        stale = artifacts.plan_cleanup(entries, NOW, retention_hours=6, max_entries=10)
        # 保底期 10 分钟外的都合规；超容 20 → 补刀最老的 20 个
        self.assertEqual(len(stale), 20)
        self.assertEqual(stale[0], "/t/29.wav", "最老的先删（29 分钟前）")
        self.assertNotIn("/t/0.wav", stale, "最新的留在台账里")

    def test_retention_candidates_count_toward_capacity_excess(self) -> None:
        entries = {f"/t/expired-{i}.wav": ts(hours=12 + i) for i in range(5)}
        entries.update({f"/t/overflow-{i}.wav": ts(minutes=20 + i) for i in range(25)})
        stale = artifacts.plan_cleanup(entries, NOW, retention_hours=6, max_entries=20)
        self.assertEqual(len(stale), 10, "先删 5 个过期项，再补足超容的 5 个")
        self.assertTrue(all(path.startswith("/t/expired-") for path in stale[:5]))
        self.assertEqual(stale[5:], [f"/t/overflow-{i}.wav" for i in range(24, 19, -1)])

    def test_mixed_naive_and_aware_timestamps_are_comparable(self) -> None:
        entries = {
            "/t/old-naive.wav": (NOW.replace(tzinfo=None) - timedelta(hours=7)).isoformat(),
            "/t/old-aware.wav": ts(hours=8),
            "/t/fresh-naive.wav": (NOW.replace(tzinfo=None) - timedelta(minutes=5)).isoformat(),
        }
        self.assertEqual(
            artifacts.plan_cleanup(entries, NOW, retention_hours=6),
            ["/t/old-aware.wav", "/t/old-naive.wav"],
        )

    def test_naive_now_uses_aware_ledger_timezone(self) -> None:
        entries = {"/t/fresh.wav": ts(minutes=5), "/t/old.wav": ts(hours=7)}
        self.assertEqual(
            artifacts.plan_cleanup(entries, NOW.replace(tzinfo=None), retention_hours=6),
            ["/t/old.wav"],
        )

    def test_bad_timestamp_never_deleted(self) -> None:
        entries = {"/t/bad.wav": "不是时间", "/t/old.wav": ts(hours=9)}
        stale = artifacts.plan_cleanup(entries, NOW)
        self.assertEqual(stale, ["/t/old.wav"], "时间戳坏了不敢删（宁多留不错删）")

    def test_sweep_cap(self) -> None:
        entries = {f"/t/{i}.wav": ts(hours=48) for i in range(300)}
        stale = artifacts.plan_cleanup(entries, NOW)
        self.assertEqual(len(stale), artifacts.MAX_DELETES_PER_SWEEP, "单轮最多 100 个")

    def test_empty_and_bad_input(self) -> None:
        self.assertEqual(artifacts.plan_cleanup({}, NOW), [])
        self.assertEqual(artifacts.plan_cleanup(None, NOW), [])

    def test_prune_removes_only_given(self) -> None:
        entries = {"/t/a.wav": ts(1), "/t/b.wav": ts(2)}
        out = artifacts.prune(entries, ["/t/a.wav"])
        self.assertEqual(out, {"/t/b.wav": ts(2)})


class RecordAndNormalizeTest(unittest.TestCase):
    def test_record_adds_and_refreshes(self) -> None:
        base = {"/t/a.wav": ts(60)}
        out = artifacts.record(base, "/t/a.wav", NOW)
        self.assertEqual(out["/t/a.wav"], NOW.isoformat(timespec="seconds"), "同路径刷新时间戳")
        self.assertEqual(base["/t/a.wav"], ts(60), "不改原对象")

    def test_record_ignores_blank_path(self) -> None:
        self.assertEqual(artifacts.record({}, "  ", NOW), {})

    def test_normalize_drops_bad_shape(self) -> None:
        self.assertEqual(artifacts.normalize(None), {})
        self.assertEqual(artifacts.normalize({"/t/a.wav": "", "": "x", "/t/b.wav": "y"}),
                         {"/t/b.wav": "y"})


class LedgerFileTest(TmpDirCase):
    def test_load_missing_file_returns_empty(self) -> None:
        self.assertEqual(artifacts.load(self.root / "nope.json"), {})

    def test_save_then_load_roundtrip(self) -> None:
        path = self.root / "ledger.json"
        entries = artifacts.record({}, "/t/a.wav", NOW)
        artifacts.save(path, entries)
        self.assertEqual(artifacts.load(path), entries)

    def test_load_bad_json_returns_empty(self) -> None:
        path = self.root / "ledger.json"
        path.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(artifacts.load(path), {})


class OwnedPathTest(TmpDirCase):
    def test_safe_owned_path_requires_descendant_of_cache_root(self) -> None:
        root = self.root / "voice-cache"
        root.mkdir()
        owned = root / "audio.wav"
        owned.write_bytes(b"audio")
        nested = root / "nested"
        nested.mkdir()
        nested_file = nested / "nested.wav"
        nested_file.write_bytes(b"nested")
        outside = self.root / "other.wav"
        outside.write_bytes(b"other")

        self.assertEqual(artifacts.safe_owned_path(owned, root), owned.resolve())
        self.assertIsNone(artifacts.safe_owned_path(root, root), "不允许删除缓存根目录")
        self.assertIsNone(artifacts.safe_owned_path(nested_file, root), "只接受缓存根目录的直接子文件")
        self.assertIsNone(artifacts.safe_owned_path(outside, root), "台账路径必须位于专属缓存内")

    def test_safe_owned_path_rejects_symlink_escape_when_supported(self) -> None:
        root = self.root / "voice-cache"
        root.mkdir()
        outside = self.root / "other.wav"
        outside.write_bytes(b"other")
        link = root / "linked.wav"
        try:
            link.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("当前 Windows 环境不允许创建符号链接")
        self.assertIsNone(artifacts.safe_owned_path(link, root))


class ClampTest(unittest.TestCase):
    def test_retention_hours(self) -> None:
        self.assertEqual(artifacts.clamp_retention_hours("12"), 12.0)
        self.assertEqual(artifacts.clamp_retention_hours(0), 1.0, "下限 1 小时")
        self.assertEqual(artifacts.clamp_retention_hours(99999), 336.0, "上限 14 天")
        self.assertEqual(artifacts.clamp_retention_hours("乱"), 6.0)

    def test_max_entries(self) -> None:
        self.assertEqual(artifacts.clamp_max_entries("500"), 500)
        self.assertEqual(artifacts.clamp_max_entries(1), 20, "下限 20")
        self.assertEqual(artifacts.clamp_max_entries(99999), 2000, "上限 2000")
        self.assertEqual(artifacts.clamp_max_entries(None), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
