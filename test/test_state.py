"""``core.state`` 的行为测试（第 3 步）：情绪两层、作息表、熟悉度漏积分器、互动轨迹。

冻结面（任务书 §2）：``from core.state import State, Affinity``；方法签名与
``state.json`` / ``affinity.json`` 键名不许漂移。日期注意：2026-10-04 是周日。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import compose  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.session import Session  # noqa: E402
from core.state import Affinity, State  # noqa: E402
from core.state.affinity import DECAY_UNIT_DAYS, LAMBDA  # noqa: E402
from core.state.store import AffinityStore, StateStore  # noqa: E402
from support import TZ, TmpDirCase, private_session  # noqa: E402

# 2026-10-04 是周日；周二用 10-06（工作日表生效）
SUN_2130 = datetime(2026, 10, 4, 21, 30, tzinfo=TZ)
TUE_0200 = datetime(2026, 10, 6, 2, 0, tzinfo=TZ)
TUE_0630 = datetime(2026, 10, 6, 6, 30, tzinfo=TZ)
TUE_0930 = datetime(2026, 10, 6, 9, 30, tzinfo=TZ)
TUE_1130 = datetime(2026, 10, 6, 11, 30, tzinfo=TZ)
TUE_1330 = datetime(2026, 10, 6, 13, 30, tzinfo=TZ)
TUE_1830 = datetime(2026, 10, 6, 18, 30, tzinfo=TZ)
TUE_2330 = datetime(2026, 10, 6, 23, 30, tzinfo=TZ)
SUN_1100 = datetime(2026, 10, 4, 11, 0, tzinfo=TZ)


def run(coro):
    return asyncio.run(coro)


def make_state(root: Path, config: dict | None = None) -> State:
    raw: dict[str, Any] = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    settings = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    return State(
        settings=settings,
        layout=layout,
        store=StateStore(layout=layout, locks=locks),
        locks=locks,
    )


def make_affinity(root: Path, config: dict | None = None) -> Affinity:
    raw: dict[str, Any] = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    settings = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    return Affinity(
        settings=settings,
        layout=layout,
        store=AffinityStore(layout=layout, locks=locks),
        locks=locks,
    )


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def seed_json(path: Path, payload: dict) -> None:
    """模拟外部进程（WebUI / 第 4 步的主动消息模块）直接写文件。"""
    storage.atomic_write_json(path, payload)


# ---- 文件层与落盘形状 -----------------------------------------------------------


class StoreTest(TmpDirCase):
    def test_state_json_shape_matches_frozen_skeleton(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", valence=-1, now=SUN_2130))
        doc = read_json(state.layout.state)
        self.assertEqual(doc["schema_version"], 1)
        mood = doc["mood"]
        self.assertEqual(mood["word"], "有点烦")
        self.assertEqual(mood["valence"], -1)
        self.assertEqual(mood["arousal"], 0)
        self.assertIn("updated_at", mood)
        baseline = mood["baseline"]
        self.assertIn("valence", baseline)
        self.assertIn("arousal", baseline)
        self.assertIn("updated_at", baseline)

    def test_affinity_json_shape_matches_frozen_skeleton(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), now=SUN_2130))
        run(affinity.store.flush())  # 合并写：断言文件前先把窗口冲掉
        doc = read_json(affinity.layout.affinity)
        self.assertEqual(doc["schema_version"], 1)
        person = doc["people"]["10001"]
        self.assertAlmostEqual(person["score"], 1.0)
        self.assertIn("last_ts", person)

    def test_fresh_store_reads_as_empty(self) -> None:
        state = make_state(self.root)
        affinity = make_affinity(self.root)
        self.assertEqual(state.mood_line(private_session(), now=SUN_2130), "")
        self.assertEqual(state.snapshot(now=SUN_2130)["mood"]["valence"], None)
        self.assertEqual(affinity.score("10001", now=SUN_2130), 0.0)
        self.assertEqual(affinity.band("10001", now=SUN_2130), "陌生")

    def test_concurrent_observes_keep_file_valid(self) -> None:
        state = make_state(self.root)

        async def scenario():
            async def one(index: int):
                await state.observe(
                    private_session(), word=f"心情{index}", valence=index % 5 - 2, now=SUN_2130
                )

            await asyncio.gather(*(one(i) for i in range(10)))

        run(scenario())
        doc = read_json(state.layout.state)
        self.assertTrue(doc["mood"]["word"])
        self.assertIn(doc["mood"]["valence"], (-2, -1, 0, 1, 2))

    def test_concurrent_touches_never_exceed_daily_cap(self) -> None:
        affinity = make_affinity(self.root)

        async def scenario():
            await asyncio.gather(
                *(
                    affinity.touch(private_session(), now=SUN_2130)
                    for _ in range(8)
                )
            )

        run(scenario())
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 3.0)

    def test_concurrent_touches_of_different_people_all_kept(self) -> None:
        affinity = make_affinity(self.root)

        async def scenario():
            await asyncio.gather(
                *(
                    affinity.touch(private_session(peer=f"2000{i}"), now=SUN_2130)
                    for i in range(6)
                )
            )

        run(scenario())
        run(affinity.store.flush())  # 合并写：窗口安静后增量才落盘
        people = read_json(affinity.layout.affinity)["people"]
        self.assertEqual(len(people), 6)
        for person in people.values():
            self.assertAlmostEqual(person["score"], 1.0)


# ---- 情绪历史（state_history.jsonl，任务书 §2.5 v0.7） --------------------------


class HistoryTest(TmpDirCase):
    FROZEN_KEYS = {"ts", "layer", "valence", "arousal", "word"}

    def read_lines(self, state: State) -> list[dict]:
        raw = state.layout.state_history.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "不许有 BOM")
        self.assertTrue(raw.endswith(b"\n") or not raw, "行尾 LF")
        out = []
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def test_first_change_appends_lines(self) -> None:
        """首次自报：坐标动了 → now 行；基调从 0 挪一格 → baseline 行。"""
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", valence=-1, now=SUN_2130))
        lines = self.read_lines(state)
        self.assertEqual([line["layer"] for line in lines], ["now", "baseline"])
        now_line = lines[0]
        self.assertEqual(set(now_line), self.FROZEN_KEYS)
        self.assertEqual(now_line["valence"], -1)
        self.assertEqual(now_line["arousal"], 0)
        self.assertEqual(now_line["word"], "有点烦")
        parsed = datetime.fromisoformat(now_line["ts"])
        self.assertIsNotNone(parsed.tzinfo, "ts 要带时区")
        self.assertEqual(lines[1]["valence"], -1.0)

    def test_same_value_repeat_observe_writes_no_line(self) -> None:
        """§4 点名：同值重复 observe → 行数不变（只刷新 state.json 时间戳）。"""
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        first = len(self.read_lines(state))
        self.assertEqual(first, 2)
        run(state.observe(private_session(), valence=-1, now=SUN_2130 + timedelta(hours=1)))
        self.assertEqual(len(self.read_lines(state)), first)
        mood = read_json(state.layout.state)["mood"]
        self.assertEqual(
            mood["updated_at"],
            (SUN_2130 + timedelta(hours=1)).isoformat(timespec="seconds"),
            "时间戳照常刷新",
        )

    def test_stale_line_pruned_on_append(self) -> None:
        """§4 点名：塞一条 200 天前的旧行后再追加 → 旧行被裁掉。"""
        state = make_state(self.root)
        old_line = {
            "ts": (SUN_2130 - timedelta(days=200)).isoformat(timespec="seconds"),
            "layer": "now",
            "valence": -2,
            "arousal": 0,
            "word": "很久以前",
        }
        fresh_line = {
            "ts": (SUN_2130 - timedelta(days=1)).isoformat(timespec="seconds"),
            "layer": "now",
            "valence": 1,
            "arousal": 0,
            "word": "还行",
        }
        storage.atomic_write_text(
            state.layout.state_history,
            json.dumps(old_line, ensure_ascii=False) + "\n"
            + json.dumps(fresh_line, ensure_ascii=False) + "\n",
        )
        run(state.observe(private_session(), valence=2, now=SUN_2130))
        lines = self.read_lines(state)
        self.assertEqual(len(lines), 3, "旧行被裁：保留期内的种子行 + 新 now 行 + 新 baseline 行")
        self.assertNotIn("很久以前", [line["word"] for line in lines])
        self.assertEqual([line["layer"] for line in lines], ["now", "now", "baseline"])
        self.assertEqual(lines[1]["valence"], 2)

    def test_both_layers_write_their_own_lines(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-2, arousal=0, now=SUN_2130))
        lines = self.read_lines(state)
        self.assertEqual([line["layer"] for line in lines], ["now", "baseline"])
        self.assertEqual(lines[0]["valence"], -2)
        self.assertEqual(lines[1]["valence"], -1.0, "基调只挪了一格")
        for line in lines:
            self.assertEqual(set(line), self.FROZEN_KEYS)

    def test_partial_axis_change_writes_one_now_line_with_both_coords(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        run(state.observe(private_session(), arousal=2, now=SUN_2130 + timedelta(hours=1)))
        lines = self.read_lines(state)
        self.assertEqual([line["layer"] for line in lines], ["now", "baseline", "now", "baseline"])
        now_lines = [line for line in lines if line["layer"] == "now"]
        self.assertEqual(len(now_lines), 2, "valence 没动 → now 行只有 arousal 变化这一行")
        self.assertEqual(
            (now_lines[-1]["valence"], now_lines[-1]["arousal"]),
            (-1, 2),
            "没动的轴带着当前值",
        )

    def test_word_only_writes_nothing(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", now=SUN_2130))
        self.assertFalse(
            state.layout.state_history.exists(), "坐标没变不写（word-only 不建文件）"
        )

    def test_zero_equals_default_writes_nothing(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=0, arousal=0, now=SUN_2130))
        self.assertFalse(state.layout.state_history.exists(), "与缺省同值＝坐标没变")

    def test_malformed_line_is_skipped_by_readers(self) -> None:
        """坏行不炸读取与追加；物理清除只发生在触发 180 天裁剪的重写时（§2.5 只要求裁剪）。"""
        state = make_state(self.root)
        good_line = {
            "ts": (SUN_2130 - timedelta(days=1)).isoformat(timespec="seconds"),
            "layer": "now",
            "valence": 1,
            "arousal": 0,
            "word": "还行",
        }
        storage.atomic_write_text(
            state.layout.state_history,
            "{这行不是 JSON}\n" + json.dumps(good_line, ensure_ascii=False) + "\n",
        )
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        lines = state.store.read_history_lines()
        self.assertTrue(lines and all(isinstance(line, dict) for line in lines))
        self.assertEqual(lines[0]["valence"], 1, "坏行对读取方不可见，好行与新行都在")

    def test_concurrent_appends_keep_all_changes(self) -> None:
        state = make_state(self.root)

        async def scenario():
            values = (-2, -1, 1, 2)

            async def one(index: int):
                await state.observe(private_session(), valence=values[index], now=SUN_2130)

            await asyncio.gather(*(one(i) for i in range(4)))

        run(scenario())
        lines = self.read_lines(state)
        now_lines = [line for line in lines if line["layer"] == "now"]
        self.assertGreaterEqual(len(now_lines), 4)
        self.assertEqual({line["valence"] for line in now_lines}, {-2, -1, 1, 2})


# ---- 情绪：自报（observe） -------------------------------------------------------


class ObserveTest(TmpDirCase):
    def test_word_only_is_stored_and_coordinates_untouched(self) -> None:
        state = make_state(self.root)
        result = run(state.observe(private_session(), word="有点烦", now=SUN_2130))
        self.assertTrue(result["ok"])
        snapshot = state.snapshot(now=SUN_2130)
        self.assertEqual(snapshot["mood"]["word"], "有点烦")
        self.assertEqual(snapshot["mood"]["valence"], 0, "没给分坐标保持中性")

    def test_coordinates_clamped_into_range(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=5, arousal=-9, now=SUN_2130))
        snapshot = state.snapshot(now=SUN_2130)
        self.assertEqual(snapshot["mood"]["valence"], 2)
        self.assertEqual(snapshot["mood"]["arousal"], -2)

    def test_missing_coordinate_never_overwrites(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        run(state.observe(private_session(), word="还行", now=SUN_2130 + timedelta(hours=1)))
        snapshot = state.snapshot(now=SUN_2130 + timedelta(hours=1))
        self.assertLess(snapshot["mood"]["valence"], -0.5, "只报词不写 0 覆盖")

    def test_word_and_coordinates_together(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="超开心", valence=2, arousal=1, now=SUN_2130))
        snapshot = state.snapshot(now=SUN_2130)
        self.assertEqual(snapshot["mood"]["word"], "超开心")
        self.assertEqual(snapshot["mood"]["valence"], 2)

    def test_observe_with_nothing_is_a_gentle_error(self) -> None:
        state = make_state(self.root)
        result = run(state.observe(private_session(), now=SUN_2130))
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_disabled_state_rejects_observe_and_lines(self) -> None:
        state = make_state(self.root, {"subsystems": {"state": False}})
        result = run(state.observe(private_session(), word="有点烦", now=SUN_2130))
        self.assertFalse(result["ok"])
        self.assertIn("关闭", result["error"])
        self.assertEqual(state.mood_line(private_session(), now=SUN_2130), "")
        self.assertEqual(state.rhythm_line(now=SUN_2130), "")
        self.assertFalse(state.is_late_night(now=TUE_2330))
        self.assertEqual(state.snapshot(now=SUN_2130), {})


class BaselineTest(TmpDirCase):
    def test_single_move_is_at_most_one_step(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-2, now=SUN_2130))
        baseline = state.snapshot(now=SUN_2130)["mood"]["baseline"]
        self.assertEqual(baseline["valence"], -1.0, "一篇很丧的日记最多拽一格")

    def test_daily_move_is_at_most_one_step(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-2, now=SUN_2130))
        # +2h 还是同一天（再往后就跨午夜了，单日封顶会被重置——那是次日的事）
        run(state.observe(private_session(), valence=-2, now=SUN_2130 + timedelta(hours=2)))
        baseline = state.snapshot(now=SUN_2130 + timedelta(hours=2))["mood"]["baseline"]
        self.assertLess(baseline["valence"], -0.9, "单日也最多一格（存量衰减后仍 ≈ -1）")
        self.assertGreater(baseline["valence"], -1.1, "没被第二次拽到 -2")

    def test_baseline_moves_again_next_day(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-2, now=SUN_2130))
        run(state.observe(private_session(), valence=-2, now=SUN_2130 + timedelta(days=1)))
        baseline = state.snapshot(now=SUN_2130 + timedelta(days=1))["mood"]["baseline"]
        self.assertLess(baseline["valence"], -1.8, "次日继续沉淀")
        self.assertGreater(baseline["valence"], -2.0)

    def test_baseline_moves_toward_positive_too(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        run(state.observe(private_session(), valence=2, now=SUN_2130 + timedelta(days=1)))
        baseline = state.snapshot(now=SUN_2130 + timedelta(days=1))["mood"]["baseline"]
        self.assertGreater(baseline["valence"], -0.2, "从 -1 向 +2 挪一格")


class DecayTest(TmpDirCase):
    def test_current_coordinates_decay_with_three_hour_half_life(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-2, arousal=0, now=SUN_2130))
        snapshot = state.snapshot(now=SUN_2130 + timedelta(hours=6))
        self.assertAlmostEqual(snapshot["mood"]["valence"], -0.5, delta=0.01)

    def test_baseline_decays_with_seven_day_half_life(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=-1, now=SUN_2130))
        snapshot = state.snapshot(now=SUN_2130 + timedelta(days=7))
        self.assertAlmostEqual(snapshot["mood"]["baseline"]["valence"], -0.5, delta=0.01)

    def test_fresh_coordinates_are_not_decayed(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), valence=2, now=SUN_2130))
        self.assertEqual(state.snapshot(now=SUN_2130)["mood"]["valence"], 2)


# ---- 情绪：注入行（mood_line） ---------------------------------------------------


class MoodLineTest(TmpDirCase):
    def test_empty_mood_renders_empty(self) -> None:
        state = make_state(self.root)
        self.assertEqual(state.mood_line(private_session(), now=SUN_2130), "")

    def test_word_shown_within_visibility_window(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", now=SUN_2130))
        self.assertIn("有点烦", state.mood_line(private_session(), now=SUN_2130))

    def test_word_gone_after_visibility_window(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", now=SUN_2130))
        line = state.mood_line(private_session(), now=SUN_2130 + timedelta(hours=7))
        self.assertEqual(line, "", "三小时前有点烦不该一直挂着")

    def test_quadrant_words_without_a_reported_word(self) -> None:
        state = make_state(self.root)
        cases = {
            (2, 2): "挺开心",
            (2, -2): "很平静",
            (-2, 2): "有点烦躁",
            (-2, -2): "有点低落",
        }
        for (v, a), phrase in cases.items():
            with self.subTest(v=v, a=a):
                fresh = make_state(self.root)
                run(fresh.observe(private_session(), valence=v, arousal=a, now=SUN_2130))
                self.assertIn(phrase, fresh.mood_line(private_session(), now=SUN_2130))

    def test_both_layers_combined(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", valence=-1, now=SUN_2130))
        line = state.mood_line(private_session(), now=SUN_2130)
        self.assertIn("最近她心情有点低沉", line)
        self.assertIn("不过这会儿有点烦", line)

    def test_baseline_only_after_current_expires(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", valence=-1, now=SUN_2130))
        line = state.mood_line(private_session(), now=SUN_2130 + timedelta(hours=7))
        self.assertEqual(line, "最近她心情有点低沉。")

    def test_cuts_baseline_first_when_over_budget(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="烦" * 40, valence=1, now=SUN_2130))
        line = state.mood_line(private_session(), now=SUN_2130)
        self.assertIn("烦" * 40, line, "保当下")
        self.assertNotIn("最近", line, "先砍基调")

    def test_no_digits_ever_reach_the_prompt(self) -> None:
        state = make_state(self.root)
        run(state.observe(private_session(), word="有点烦", valence=-2, now=SUN_2130))
        line = state.mood_line(private_session(), now=SUN_2130)
        self.assertFalse(any(ch.isdigit() for ch in line), line)


# ---- 作息（rhythm） -------------------------------------------------------------


class RhythmTest(TmpDirCase):
    def test_weekday_table_segments(self) -> None:
        state = make_state(self.root)
        expected = [
            (TUE_0630, "刚醒"),
            (TUE_0930, "精神不错"),
            (TUE_1330, "有点犯困"),
            (TUE_1830, "晚饭后放松"),
            (TUE_2330, "该睡了"),
        ]
        for moment, word in expected:
            with self.subTest(hour=moment.hour):
                self.assertIn(word, state.rhythm_line(now=moment))

    def test_wrap_before_first_segment_goes_to_last(self) -> None:
        state = make_state(self.root)
        self.assertIn("该睡了", state.rhythm_line(now=TUE_0200))

    def test_line_reads_like_a_sentence(self) -> None:
        state = make_state(self.root)
        self.assertEqual(state.rhythm_line(now=TUE_2330), "现在这个点，她该睡了")

    def test_custom_table_and_fullwidth_pipe(self) -> None:
        state = make_state(self.root, {"state": {"rhythm": "07:00｜夜猫子"}})
        self.assertIn("夜猫子", state.rhythm_line(now=TUE_0930))

    def test_empty_table_renders_empty(self) -> None:
        state = make_state(self.root, {"state": {"rhythm": ""}})
        self.assertEqual(state.rhythm_line(now=TUE_1330), "")

    def test_weekend_table_used_on_sunday(self) -> None:
        state = make_state(self.root, {"state": {"rhythm_weekend": "10:00|睡到自然醒"}})
        self.assertIn("睡到自然醒", state.rhythm_line(now=SUN_1100))

    def test_weekday_table_used_on_tuesday_even_with_weekend_table(self) -> None:
        state = make_state(self.root, {"state": {"rhythm_weekend": "10:00|睡到自然醒"}})
        self.assertIn("精神不错", state.rhythm_line(now=TUE_1130))

    def test_weekend_falls_back_to_weekday_table_when_empty(self) -> None:
        state = make_state(self.root)
        self.assertIn("精神不错", state.rhythm_line(now=SUN_1100), "没配周末表就沿用工作日")


class LateNightTest(TmpDirCase):
    def test_default_window_crosses_midnight(self) -> None:
        state = make_state(self.root)
        cases = [
            (datetime(2026, 10, 6, 23, 59, tzinfo=TZ), True),
            (datetime(2026, 10, 6, 3, 0, tzinfo=TZ), True),
            (datetime(2026, 10, 6, 6, 29, tzinfo=TZ), True),
            (TUE_0630, False),
            (TUE_1330, False),
        ]
        for moment, expected in cases:
            with self.subTest(hour=moment.hour, minute=moment.minute):
                self.assertEqual(state.is_late_night(now=moment), expected)

    def test_custom_window(self) -> None:
        state = make_state(self.root, {"state": {"late_night": "01:00-05:00"}})
        self.assertTrue(state.is_late_night(now=datetime(2026, 10, 6, 3, 0, tzinfo=TZ)))
        self.assertFalse(state.is_late_night(now=datetime(2026, 10, 6, 0, 30, tzinfo=TZ)))
        self.assertFalse(state.is_late_night(now=datetime(2026, 10, 6, 5, 0, tzinfo=TZ)))

    def test_invalid_or_empty_window_is_never_late(self) -> None:
        for value in ("", "garbage", "25:00-26:00", "abc-def"):
            with self.subTest(value=value):
                state = make_state(self.root, {"state": {"late_night": value}})
                self.assertFalse(state.is_late_night(now=datetime(2026, 10, 6, 3, 0, tzinfo=TZ)))

    def test_snapshot_carries_rhythm_and_late_night(self) -> None:
        state = make_state(self.root)
        snapshot = state.snapshot(now=TUE_2330)
        self.assertEqual(snapshot["rhythm"]["word"], "该睡了")
        self.assertTrue(snapshot["rhythm"]["late_night"])


# ---- 熟悉度（Affinity） ---------------------------------------------------------


class TouchTest(TmpDirCase):
    def test_private_touch_counts_full_delta(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), kind="private", now=SUN_2130))
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 1.0)

    def test_mention_touch_counts_partial_delta(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), kind="mention", now=SUN_2130))
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 0.6)

    def test_group_is_the_same_tier_as_mention(self) -> None:
        """权重梯子只有两档（复核 #4）：group 是"群里被叫醒接话"的别名。"""
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), kind="group", now=SUN_2130))
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 0.6)

    def test_unknown_kind_falls_to_group_tier(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), kind="party", now=SUN_2130))
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 0.6)

    def test_decay_is_by_elapsed_time_not_by_touch_count(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), now=SUN_2130))
        run(affinity.touch(private_session(), now=SUN_2130))
        self.assertAlmostEqual(
            affinity.score("10001", now=SUN_2130),
            2.0,
            msg="同一时刻两次 touch 不该各乘一次 λ（复核 #3）",
        )
        self.assertAlmostEqual(
            affinity.score("10001", now=SUN_2130 + timedelta(days=DECAY_UNIT_DAYS)), 1.0
        )

    def test_daily_cap_limits_farming(self) -> None:
        affinity = make_affinity(self.root)
        for index in range(5):
            run(affinity.touch(private_session(), now=SUN_2130 + timedelta(minutes=index)))
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 3.0, delta=0.01)

    def test_cap_resets_next_day(self) -> None:
        affinity = make_affinity(self.root)
        for index in range(3):
            run(affinity.touch(private_session(), now=SUN_2130 + timedelta(minutes=index)))
        run(affinity.touch(private_session(), now=SUN_2130 + timedelta(days=1)))
        expected = 3.0 * (0.5 ** (1 / DECAY_UNIT_DAYS)) + 1.0
        self.assertAlmostEqual(
            affinity.score("10001", now=SUN_2130 + timedelta(days=1)), expected, delta=0.01
        )

    def test_disabled_state_ignores_touches(self) -> None:
        affinity = make_affinity(self.root, {"subsystems": {"state": False}})
        run(affinity.touch(private_session(), now=SUN_2130))
        self.assertFalse(affinity.layout.affinity.exists())
        self.assertEqual(affinity.score("10001", now=SUN_2130), 0.0)

    def test_touch_without_person_is_ignored(self) -> None:
        affinity = make_affinity(self.root)
        run(affinity.touch(Session(), now=SUN_2130))
        self.assertFalse(affinity.layout.affinity.exists())


class MergedWriteTest(TmpDirCase):
    """返工轮：touch 的合并写窗口（1.5 秒），同会话连续互动只落一次盘。"""

    def test_score_reads_pending_without_flush(self) -> None:
        affinity = make_affinity(self.root)

        async def scenario():
            await affinity.touch(private_session(), now=SUN_2130)
            self.assertFalse(
                affinity.layout.affinity.exists(), "窗口内还没有落盘"
            )
            self.assertAlmostEqual(
                affinity.score("10001", now=SUN_2130), 1.0, msg="读数走 overlay，不等落盘"
            )

        run(scenario())

    def test_repeated_touches_in_window_hit_disk_once(self) -> None:
        affinity = make_affinity(self.root)
        calls: list[int] = []
        original = affinity.store._save

        def spy(doc):
            calls.append(1)
            original(doc)

        affinity.store._save = spy  # type: ignore[method-assign]

        async def scenario():
            for index in range(5):
                await affinity.touch(
                    private_session(), now=SUN_2130 + timedelta(seconds=index)
                )
            self.assertEqual(calls, [], "窗口内一次都不写")
            self.assertTrue(await affinity.store.flush())

        run(scenario())
        self.assertEqual(len(calls), 1, "五次互动合并成一次落盘")
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 3.0, delta=0.01)

    def test_flush_persists_exact_math(self) -> None:
        """合并写的最终值与写穿完全一致（overlay 上的衰减/封顶连续演化）。"""
        affinity = make_affinity(self.root)

        async def scenario():
            for index in range(5):
                await affinity.touch(
                    private_session(), now=SUN_2130 + timedelta(seconds=index)
                )
            await affinity.store.flush()

        run(scenario())
        doc = read_json(affinity.layout.affinity)
        self.assertAlmostEqual(doc["people"]["10001"]["score"], 3.0, delta=0.01)
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 3.0, delta=0.01)

    def test_concurrent_touch_and_flush_keep_all_increments(self) -> None:
        """flush 换出 overlay 与落盘全程持锁：窗口中途到来的 touch 不丢增量。"""
        affinity = make_affinity(self.root)

        async def scenario():
            await affinity.touch(private_session(), now=SUN_2130)
            await asyncio.gather(
                affinity.store.flush(),
                affinity.touch(private_session(), now=SUN_2130),
            )
            await affinity.store.flush()

        run(scenario())
        self.assertAlmostEqual(affinity.score("10001", now=SUN_2130), 2.0, delta=0.01)
        doc = read_json(affinity.layout.affinity)
        self.assertAlmostEqual(doc["people"]["10001"]["score"], 2.0, delta=0.01)


class DecayCriteriaTest(TmpDirCase):
    """返工轮判据：一周不聊不该掉档，一个月不见才该明显降（半衰期 21 天，待调）。"""

    def seed(self, score: float) -> Affinity:
        affinity = make_affinity(self.root)
        seed_json(
            affinity.layout.affinity,
            {"people": {"10001": {"score": score, "last_ts": SUN_2130.isoformat()}}},
        )
        return affinity

    def balanced(self) -> float:
        """每天一轮私聊（Δ=1/天）的漏积分器平衡分。"""
        return 1.0 / (1.0 - LAMBDA ** (1 / DECAY_UNIT_DAYS))

    def test_week_off_does_not_drop_a_band(self) -> None:
        week = timedelta(days=7)
        self.assertEqual(
            self.seed(self.balanced()).band("10001", now=SUN_2130 + week),
            "熟稔",
            f"稳定互动者（平衡分 ≈{self.balanced():.1f}）一周不聊仍在熟稔档",
        )
        self.assertEqual(self.seed(5.0).band("10001", now=SUN_2130 + week), "泛泛")
        self.assertEqual(self.seed(9.0).band("10001", now=SUN_2130 + week), "泛泛")

    def test_month_absence_drops_band(self) -> None:
        self.assertEqual(
            self.seed(5.0).band("10001", now=SUN_2130 + timedelta(days=30)),
            "陌生",
            "泛泛中位一个月不见降到陌生",
        )
        self.assertEqual(
            self.seed(9.0).band("10001", now=SUN_2130 + timedelta(days=60)), "陌生"
        )
        self.assertEqual(
            self.seed(self.balanced()).band("10001", now=SUN_2130 + timedelta(days=60)),
            "泛泛",
            "熟稔要两个月不聊才降档",
        )


class BandTest(TmpDirCase):
    def seed(self, score: float) -> Affinity:
        affinity = make_affinity(self.root)
        seed_json(
            affinity.layout.affinity,
            {
                "people": {
                    # last_ts 就取当前时刻：带衰减的边界值留给 DecayTest 去测
                    "10001": {"score": score, "last_ts": SUN_2130.isoformat()}
                }
            },
        )
        return affinity

    def test_band_words_are_exactly_the_frozen_three(self) -> None:
        for score, word in ((0.0, "陌生"), (3.0, "泛泛"), (9.99, "泛泛"), (12.0, "熟稔")):
            with self.subTest(score=score):
                self.assertEqual(self.seed(score).band("10001", now=SUN_2130), word)

    def test_band_boundary_at_ten_is_close(self) -> None:
        self.assertEqual(self.seed(10.0).band("10001", now=SUN_2130), "熟稔")

    def test_unknown_person_is_a_stranger(self) -> None:
        affinity = make_affinity(self.root)
        self.assertEqual(affinity.band("nobody", now=SUN_2130), "陌生")

    def test_negative_scores_never_go_below_zero(self) -> None:
        affinity = self.seed(-5.0)
        self.assertEqual(affinity.score("10001", now=SUN_2130), 0.0)

    def test_line_contains_the_band_word_and_no_numbers(self) -> None:
        for score, word in ((12.0, "熟稔"), (5.0, "泛泛"), (0.0, "陌生")):
            with self.subTest(score=score):
                affinity = self.seed(score)
                line = affinity.line(private_session(), now=SUN_2130)
                self.assertIn(word, line)
                self.assertFalse(any(ch.isdigit() for ch in line), line)

    def test_line_is_empty_without_a_person(self) -> None:
        affinity = make_affinity(self.root)
        self.assertEqual(affinity.line(Session(), now=SUN_2130), "")


class TopTest(TmpDirCase):
    def seed_three(self) -> Affinity:
        affinity = make_affinity(self.root)
        seed_json(
            affinity.layout.affinity,
            {
                "people": {
                    "a": {"score": 5.0, "last_ts": (SUN_2130 - timedelta(hours=1)).isoformat()},
                    "b": {"score": 12.0, "last_ts": (SUN_2130 - timedelta(hours=1)).isoformat()},
                    "c": {"score": 0.0, "last_ts": (SUN_2130 - timedelta(hours=1)).isoformat()},
                }
            },
        )
        return affinity

    def test_top_orders_desc_and_limits(self) -> None:
        top = self.seed_three().top(limit=2, now=SUN_2130)
        self.assertEqual([item["id"] for item in top], ["b", "a"])
        self.assertGreater(top[0]["score"], top[1]["score"])

    def test_top_excludes_decayed_out_people(self) -> None:
        top = self.seed_three().top(limit=5, now=SUN_2130)
        self.assertNotIn("c", [item["id"] for item in top])


# ---- 互动轨迹（trace_line） ------------------------------------------------------


class TraceTest(TmpDirCase):
    UMO = "aiocqhttp:FriendMessage:10001"

    def seed_person(self, affinity: Affinity, last_ts: datetime) -> None:
        seed_json(
            affinity.layout.affinity,
            {"people": {"10001": {"score": 3.0, "last_ts": last_ts.isoformat()}}},
        )

    def seed_proactive(self, affinity: Affinity, last_sent_at: datetime | None) -> None:
        payload: dict[str, Any] = {}
        if last_sent_at is not None:
            payload["sessions"] = {self.UMO: {"last_sent_at": last_sent_at.isoformat()}}
        seed_json(affinity.layout.proactive, payload)

    def test_degrades_to_empty_without_proactive_file(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        self.assertEqual(affinity.trace_line(private_session(), now=SUN_2130), "")

    def test_degrades_on_malformed_proactive_file(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        affinity.layout.proactive.write_text("这不是 JSON", encoding="utf-8")
        self.assertEqual(affinity.trace_line(private_session(), now=SUN_2130), "")

    def test_degrades_on_missing_field(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        self.seed_proactive(affinity, None)
        self.assertEqual(affinity.trace_line(private_session(), now=SUN_2130), "")

    def test_degrades_when_person_never_touched(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_proactive(affinity, SUN_2130 - timedelta(days=1))
        self.assertEqual(affinity.trace_line(private_session(), now=SUN_2130), "")

    def test_degrades_on_unparsable_timestamp(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        seed_json(
            affinity.layout.proactive,
            {"sessions": {self.UMO: {"last_sent_at": "not-a-date"}}},
        )
        self.assertEqual(affinity.trace_line(private_session(), now=SUN_2130), "")

    def test_full_line_when_both_timestamps_present_and_answered(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        self.seed_proactive(affinity, SUN_2130 - timedelta(days=2))
        line = affinity.trace_line(private_session(), now=SUN_2130)
        self.assertIn("主动找过你", line)
        self.assertIn("又聊过", line)
        self.assertIn("上次聊天", line)
        self.assertLessEqual(len(line), 60)

    def test_unanswered_when_no_interaction_after_her_message(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130 - timedelta(days=3))
        self.seed_proactive(affinity, SUN_2130 - timedelta(days=1))
        line = affinity.trace_line(private_session(), now=SUN_2130)
        self.assertIn("还没回", line)
        self.assertIn("3天前", line)

    def test_lookup_prefers_umo_over_session_id(self) -> None:
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130)
        seed_json(
            affinity.layout.proactive,
            {
                "sessions": {
                    self.UMO: {"last_sent_at": (SUN_2130 - timedelta(days=2)).isoformat()},
                    "10001": {"last_sent_at": (SUN_2130 - timedelta(days=9)).isoformat()},
                }
            },
        )
        line = affinity.trace_line(private_session(), now=SUN_2130)
        self.assertIn("前天", line, "命中的是 umo 那条（前天），不是 session_id 那条（9 天前）")

    def test_pending_window_says_just_reached_out(self) -> None:
        """第 4 步 §3.3：她刚发完还没等到回音的窗口内（默认 6h），不演被冷落。"""
        affinity = make_affinity(self.root)
        self.seed_person(affinity, SUN_2130 - timedelta(days=3))
        self.seed_proactive(affinity, SUN_2130 - timedelta(hours=1))
        line = affinity.trace_line(private_session(), now=SUN_2130)
        self.assertIn("刚主动找过你", line)
        self.assertNotIn("还没回", line)

    def test_pending_window_config_wins_over_constant(self) -> None:
        """配置 ``proactive.pending_window_hours`` 存在时以配置为准。"""
        affinity = make_affinity(self.root, config={"proactive": {"pending_window_hours": 1}})
        self.seed_person(affinity, SUN_2130 - timedelta(days=3))
        self.seed_proactive(affinity, SUN_2130 - timedelta(hours=2))
        line = affinity.trace_line(private_session(), now=SUN_2130)
        self.assertIn("还没回", line, "2 小时已出 1 小时窗口 → 允许说还没回")


# ---- compose 挂载（第 3 步） -----------------------------------------------------


class _FakeDiary:
    def __init__(self, line: str = "", hint: str = "") -> None:
        self.line = line
        self.hint = hint

    def prompt_line(self, session, *, now=None) -> str:
        return self.line

    def event_hint(self, session, *, strong_text="", exchange_who="", now=None) -> str:
        return self.hint


class _FakeNotebook:
    def __init__(self, promise: str = "", fact: str = "") -> None:
        self.promise = promise
        self.fact = fact

    def promise_line(self, session, *, now=None) -> str:
        return self.promise

    def fact_line(self, session, *, now=None) -> str:
        return self.fact


class _FakeState:
    def __init__(self, mood: str = "", rhythm: str = "") -> None:
        self.mood = mood
        self.rhythm = rhythm

    def mood_line(self, session, *, now=None) -> str:
        return self.mood

    def rhythm_line(self, *, now=None) -> str:
        return self.rhythm


class _FakeAffinity:
    def __init__(self, line: str = "", trace: str = "") -> None:
        self.line_text = line
        self.trace = trace

    def line(self, session, *, now=None) -> str:
        return self.line_text

    def trace_line(self, session, *, now=None) -> str:
        return self.trace


class ComposeStep3Test(TmpDirCase):
    SESSION = private_session()

    def test_budget_is_400(self) -> None:
        self.assertEqual(compose.PROMPT_BUDGET, 400)

    def test_priority_orders_promise_fact_trace_state_diary(self) -> None:
        priority = compose.PRIORITY
        self.assertGreater(priority["promise"], priority["fact"])
        self.assertGreater(priority["fact"], priority["trace"])
        self.assertGreater(priority["trace"], priority["state"])
        self.assertGreater(priority["state"], priority["diary"])

    def test_fuse_without_state_and_affinity_matches_step2(self) -> None:
        """不回归保险丝：state/affinity 都不传时与第 2 步逐字节相同。"""
        diary = _FakeDiary(line="日记那行", hint="提醒那句")
        notebook = _FakeNotebook(promise="约定那段", fact="事实那段")
        expected = "约定那段\n事实那段\n提醒那句\n日记那行\n"
        self.assertEqual(
            compose.compose_prompt(diary, self.SESSION, notebook=notebook), expected
        )
        self.assertEqual(
            compose.compose_prompt(
                diary, self.SESSION, notebook=notebook, state=None, affinity=None
            ),
            expected,
        )

    def test_band_word_rides_the_fact_section(self) -> None:
        """熟悉度档位词与"关于当前人的事实"同档（§2.6 的表：100 字含档位词）。"""
        notebook = _FakeNotebook(fact="事实那段")
        affinity = _FakeAffinity(line="（泛泛之交）")
        text = compose.compose_prompt(
            diary=_FakeDiary(),
            session=self.SESSION,
            notebook=notebook,
            affinity=affinity,
        )
        self.assertIn("事实那段\n（泛泛之交）", text)

    def test_state_lines_are_mounted(self) -> None:
        state = _FakeState(rhythm="现在这个点，她该睡了", mood="这会儿她有点烦。")
        text = compose.compose_prompt(
            diary=_FakeDiary(line="日记那行"), session=self.SESSION, state=state
        )
        self.assertIn("现在这个点，她该睡了", text)
        self.assertIn("这会儿她有点烦", text)
        self.assertLess(text.index("她该睡了"), text.index("日记那行"))

    def test_trace_section_is_mounted(self) -> None:
        affinity = _FakeAffinity(trace="她昨天主动找过你，你还没回她。")
        text = compose.compose_prompt(
            diary=_FakeDiary(), session=self.SESSION, affinity=affinity
        )
        self.assertIn("还没回", text)

    def test_five_full_categories_are_cut_in_reverse_priority(self) -> None:
        """五类同时写满时按砍序整档丢弃：日记 → 状态 → 轨迹 → 事实；约定不砍。"""
        text = compose.compose_prompt(
            diary=_FakeDiary(line="D" * 50),
            session=self.SESSION,
            notebook=_FakeNotebook(promise="P" * 200, fact="F" * 150),
            state=_FakeState(mood="S" * 70),
            affinity=_FakeAffinity(trace="T" * 80),
        )
        expected = "P" * 200 + "\n" + "F" * 150 + "\n"
        self.assertEqual(text, expected, "350 + 80 > 400，轨迹整档砍掉，状态与日记更早")
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)

    def test_real_objects_degrade_gracefully_in_compose(self) -> None:
        """真实 State / Affinity（无 proactive.json、无情绪）不炸、不占预算。"""
        from support import make_diary

        diary = make_diary(self.root)
        state = make_state(self.root)
        affinity = make_affinity(self.root)
        text = compose.compose_prompt(
            diary, private_session(), state=state, affinity=affinity, now=TUE_1330
        )
        self.assertIn("陌生", text, "touch 之前也有档位词（陌生）")
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)

    def test_real_objects_after_touch_and_report(self) -> None:
        from support import make_diary

        diary = make_diary(self.root)
        state = make_state(self.root)
        affinity = make_affinity(self.root)
        run(affinity.touch(private_session(), now=TUE_1330))
        run(state.observe(private_session(), word="有点烦", valence=-1, now=TUE_1330))
        text = compose.compose_prompt(
            diary, private_session(), state=state, affinity=affinity, now=TUE_1330
        )
        self.assertIn("有点烦", text)
        self.assertIn("陌生人", text)
        self.assertIn("现在这个点，她有点犯困", text)
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)


if __name__ == "__main__":
    unittest.main(verbosity=2)
