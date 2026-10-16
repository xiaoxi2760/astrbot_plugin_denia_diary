"""早晚安记录（``core/greeting``）的行为测试：识别 / 记录 / 连续与睡眠时长 / 注入素材。

时间全部钉死（不准走真挂钟——凌晨与白天归属不同，和"23:30~06:30 必红"是同一类坑）。
"""

from __future__ import annotations

import asyncio
import re
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
sys.path.insert(0, str(_P(__file__).resolve().parent))

from support import TmpDirCase, private_session  # noqa: E402

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.greeting.api import (  # noqa: E402
    MORNING_PATTERNS_DEFAULT,
    NIGHT_PATTERNS_DEFAULT,
    PATTERN_FLAGS,
    Greeting,
    classify,
    effective_day,
)
from core.greeting.store import GreetingStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")


def _at(hour: int, minute: int = 0, *, day: int = 11, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TZ)


def _default_patterns() -> tuple[tuple[re.Pattern, ...], tuple[re.Pattern, ...]]:
    return (
        tuple(re.compile(p, PATTERN_FLAGS) for p in MORNING_PATTERNS_DEFAULT),
        tuple(re.compile(p, PATTERN_FLAGS) for p in NIGHT_PATTERNS_DEFAULT),
    )


class ClassifyTest(unittest.TestCase):
    """识别面：整条消息完全匹配才算，宁漏勿误伤。"""

    MORNING = ("早安", "早", "早呀", "早吖~", "早上好！", "早上好啊", "起床啦", "起床了。", "醒了", "醒啦~", "gm", "GM")
    NIGHT = ("晚安", "晚安安", "晚安呀~", "安安", "睡了", "睡啦。", "睡觉了", "睡觉啦~", "去睡了", "gn", "GN")

    def test_default_morning_words(self) -> None:
        morning, night = _default_patterns()
        for text in self.MORNING:
            self.assertEqual(classify(text, morning=morning, night=night), "morning", text)

    def test_default_night_words(self) -> None:
        morning, night = _default_patterns()
        for text in self.NIGHT:
            self.assertEqual(classify(text, morning=morning, night=night), "night", text)

    def test_chat_text_is_never_a_greeting(self) -> None:
        """聊天里提到"早安/晚安"两个字、长句、空串一律不算。"""
        morning, night = _default_patterns()
        for text in (
            "我昨天说了早安然后出门了",
            "早安世界",
            "今天早点睡",
            "早八人真的好累",
            "晚安故事讲给我听",
            "我醒了，但是不想起",
            "睡了个好觉",
            "gm ay",
            "",
            "   ",
        ):
            self.assertEqual(classify(text, morning=morning, night=night), "", repr(text))

    def test_bad_regex_is_skipped_silently_at_runtime(self) -> None:
        """坏正则在 load_settings 已报警；运行时编译层直接跳过、不抛异常。"""
        from core.greeting.api import _compiled

        good, _ = _default_patterns()
        mixed = _compiled((MORNING_PATTERNS_DEFAULT[0], "(", "["))
        self.assertEqual(len(mixed), 1, "坏条目跳过，好条目保留")
        self.assertEqual(classify("早安", morning=mixed, night=()), "morning")

    def test_effective_day_moves_early_night_to_previous_day(self) -> None:
        self.assertEqual(effective_day(_at(0, 30), "night"), "2026-10-10")
        self.assertEqual(effective_day(_at(3, 59), "night"), "2026-10-10")
        self.assertEqual(effective_day(_at(4, 0), "night"), "2026-10-11")
        self.assertEqual(effective_day(_at(23, 50), "night"), "2026-10-11")
        self.assertEqual(effective_day(_at(0, 30), "morning"), "2026-10-11")


def _make_greeting(root: Path, config: dict | None = None) -> Greeting:
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    resolved = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    return Greeting(
        settings=resolved,
        layout=layout,
        store=GreetingStore(layout=layout, locks=locks),
        locks=locks,
    )


class ObserveTest(TmpDirCase):
    def test_first_morning_is_recorded(self) -> None:
        g = _make_greeting(self.root)
        result = asyncio.run(
            g.observe(private_session("10001"), text="早安", now=_at(8))
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["kind"], "morning")
        self.assertEqual(result["date"], "2026-10-11")
        self.assertEqual(result["streak"], 1)
        self.assertIsNone(result["sleep_hours"])
        events = g.store.people()["10001"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "morning")

    def test_same_day_repeat_is_duplicate(self) -> None:
        """当日首次为准：同一天再说同款不记第二条（sleep_tracker 的"多次刷新"坑）。"""
        g = _make_greeting(self.root)
        asyncio.run(g.observe(private_session("10001"), text="早安", now=_at(8)))
        again = asyncio.run(
            g.observe(private_session("10001"), text="早上好", now=_at(11))
        )
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "duplicate")
        self.assertEqual(len(g.store.people()["10001"]["events"]), 1)

    def test_rapid_repeat_is_cooldown(self) -> None:
        """冷却内的重复连事件都不算——她不该把同一句问候回应两遍。"""
        g = _make_greeting(self.root)
        asyncio.run(g.observe(private_session("10001"), text="早安", now=_at(8)))
        again = asyncio.run(
            g.observe(private_session("10001"), text="早", now=_at(8, 10))
        )
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "cooldown")

    def test_streak_counts_consecutive_days(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        for day in (9, 10, 11):
            result = asyncio.run(
                g.observe(session, text="早安", now=_at(8, day=day))
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["streak"], day - 8)

    def test_night_and_morning_are_counted_separately(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        morning = asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        self.assertEqual(morning["streak"], 1, "早安的连续只数早安")

    def test_non_greeting_text_is_ignored(self) -> None:
        g = _make_greeting(self.root)
        result = asyncio.run(
            g.observe(private_session("10001"), text="你好呀", now=_at(8))
        )
        self.assertEqual(result["reason"], "not_greeting")
        self.assertEqual(g.store.people(), {})

    def test_disabled_subsystem_records_nothing(self) -> None:
        g = _make_greeting(self.root, {"subsystems": {"greeting": False}})
        result = asyncio.run(
            g.observe(private_session("10001"), text="早安", now=_at(8))
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "disabled")

    def test_night_before_4am_belongs_to_previous_day(self) -> None:
        """凌晨的晚安归前一天：0:30 的晚安 + 23:50 的晚安（同日历日）= 连续 2 天。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        early = asyncio.run(g.observe(session, text="晚安", now=_at(0, 30, day=11)))
        self.assertEqual(early["date"], "2026-10-10")
        late = asyncio.run(g.observe(session, text="晚安", now=_at(23, 50, day=11)))
        self.assertEqual(late["date"], "2026-10-11")
        self.assertEqual(late["streak"], 2)

    def test_sleep_hours_reported_in_reasonable_window(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        morning = asyncio.run(g.observe(session, text="早安", now=_at(7, 30, day=11)))
        self.assertTrue(morning["ok"])
        self.assertEqual(morning["sleep_hours"], 8.5)

    def test_sleep_hours_none_when_outside_window(self) -> None:
        """夜猫子（>14h）与回笼觉（<1h）不提时长——宁可不说，不可说错。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(15, day=10)))
        owl = asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        self.assertEqual(owl["sleep_hours"], None)  # 17 小时：算不准就不提
        asyncio.run(g.observe(session, text="晚安", now=_at(7, 30, day=12)))
        short = asyncio.run(g.observe(session, text="早安", now=_at(8, day=12)))
        self.assertEqual(short["sleep_hours"], None)  # 0.5 小时：不算睡眠

    def test_sleep_hours_ignores_nights_after_the_morning(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=11)))
        result = asyncio.run(g.observe(session, text="早安", now=_at(9, day=12)))
        self.assertEqual(result["sleep_hours"], 10.0)  # 只认这次早安之前的晚安

    def test_events_are_trimmed_to_cap(self) -> None:
        from datetime import timedelta

        from core.greeting.store import KEEP_EVENTS

        g = _make_greeting(self.root)
        session = private_session("10001")
        start = datetime(2026, 6, 1, tzinfo=TZ)
        for offset in range(61):  # 61 天 × 早安晚安
            moment = start + timedelta(days=offset)
            asyncio.run(g.observe(session, text="早安", now=moment.replace(hour=8)))
            asyncio.run(g.observe(session, text="晚安", now=moment.replace(hour=23)))
        events = g.store.people()["10001"]["events"]
        self.assertLessEqual(len(events), KEEP_EVENTS)


class LineTest(TmpDirCase):
    def test_line_right_after_morning_with_sleep(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        asyncio.run(g.observe(session, text="早安", now=_at(7, 30, day=11)))
        line = g.line(session, now=_at(7, 30, day=11).replace(second=30))
        self.assertIn("刚说了早安", line)
        self.assertIn("连续 1 天", line)  # 早安的连续只数早安：目前只有 11 号这一次
        self.assertIn("睡了约 8.5 小时", line)
        self.assertLessEqual(len(line), 60)

    def test_streak_in_line_counts_only_mornings(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=10)))
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        line = g.line(session, now=_at(8, 1, day=11))
        self.assertIn("连续 2 天", line)

    def test_line_for_night(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=11)))
        line = g.line(session, now=_at(23, 1, day=11))
        self.assertIn("刚道了晚安", line)
        self.assertIn("连续 1 天", line)

    def test_line_persists_all_day_after_fresh_window(self) -> None:
        """全天知晓：早安过了新鲜窗，当天其余轮次仍有背景行——但措辞框架明说"别主动复述"（反复读）。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        later = g.line(session, now=_at(8, 5, day=11))  # 5 分钟：新鲜窗已过
        self.assertNotIn("刚说了", later)
        self.assertIn("背景", later)
        self.assertIn("今早 8:00 道过早安", later)
        self.assertIn("别主动复述", later)
        self.assertLessEqual(len(later), 60)

    def test_all_day_line_does_not_leak_into_next_day(self) -> None:
        """昨天的早安今天不再驻留（明早的新问候会重新算睡眠时长）。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=10)))
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        self.assertEqual(g.line(session, now=_at(15, day=11)), "")

    def test_all_day_line_empty_when_only_night_today(self) -> None:
        """晚安不全天驻留：新鲜窗内出当轮素材，窗过即空（它由明早的睡眠时长消费）。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=10)))
        fresh = g.line(session, now=_at(23, 1, day=10))
        self.assertIn("刚道了晚安", fresh)
        expired = g.line(session, now=_at(23, 5, day=10))
        self.assertEqual(expired, "")

    def test_line_empty_without_events(self) -> None:
        g = _make_greeting(self.root)
        self.assertEqual(g.line(private_session("10001"), now=_at(8)), "")

    def test_line_empty_when_disabled(self) -> None:
        g = _make_greeting(self.root, {"subsystems": {"greeting": False}})
        self.assertEqual(g.line(private_session("10001"), now=_at(8)), "")

    def test_line_is_per_person(self) -> None:
        g = _make_greeting(self.root)
        asyncio.run(g.observe(private_session("10001"), text="早安", now=_at(8)))
        self.assertEqual(g.line(private_session("20002"), now=_at(8, 1)), "")


class SettingsTest(unittest.TestCase):
    def test_defaults_load(self) -> None:
        resolved = settings_mod.load_settings({})
        self.assertEqual(resolved.greeting["duplicate_minutes"], 30)
        self.assertTrue(resolved.greeting["morning_patterns"])
        self.assertTrue(resolved.greeting["night_patterns"])
        self.assertTrue(resolved.subsystem("greeting"))

    def test_bad_pattern_dropped_but_good_ones_kept(self) -> None:
        resolved = settings_mod.load_settings(
            {"greeting": {"morning_patterns": ["早安", "("]}}
        )
        self.assertEqual(resolved.greeting["morning_patterns"], ["早安"])
        self.assertTrue(
            any("不合法的正则" in w for w in resolved.warnings), resolved.warnings
        )

    def test_all_bad_patterns_fall_back_to_defaults(self) -> None:
        resolved = settings_mod.load_settings(
            {"greeting": {"night_patterns": ["(", "["]}}
        )
        self.assertEqual(resolved.greeting["night_patterns"], list(NIGHT_PATTERNS_DEFAULT))

    def test_patterns_not_a_list_falls_back(self) -> None:
        resolved = settings_mod.load_settings({"greeting": {"morning_patterns": "早安"}})
        self.assertEqual(resolved.greeting["morning_patterns"], list(MORNING_PATTERNS_DEFAULT))

    def test_duplicate_minutes_clamped(self) -> None:
        resolved = settings_mod.load_settings({"greeting": {"duplicate_minutes": 99999}})
        self.assertEqual(resolved.greeting["duplicate_minutes"], 1440)


class ZeroCooldownTest(TmpDirCase):
    def test_zero_minutes_means_no_cooldown_but_first_of_day_still_wins(self) -> None:
        g = _make_greeting(self.root, {"greeting": {"duplicate_minutes": 0}})
        session = private_session("10001")
        asyncio.run(g.observe(session, text="早安", now=_at(8)))
        again = asyncio.run(g.observe(session, text="早安", now=_at(9)))
        self.assertEqual(again["reason"], "duplicate")  # 当日首次为准仍然生效


class ReportTest(TmpDirCase):
    """作息报告（``sleep_check`` 工具的数据面）。"""

    def test_report_empty_for_unknown_person(self) -> None:
        g = _make_greeting(self.root)
        result = g.report("10001", now=_at(8))
        self.assertFalse(result["ok"])
        self.assertIn("还没记到", result["error"])

    def test_report_aggregates_by_day_desc(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        asyncio.run(g.observe(session, text="晚安", now=_at(23, day=9)))
        asyncio.run(g.observe(session, text="早安", now=_at(7, 30, day=10)))
        asyncio.run(g.observe(session, text="晚安", now=_at(23, 40, day=10)))
        asyncio.run(g.observe(session, text="早安", now=_at(8, day=11)))
        result = g.report(session.person_id(), now=_at(9, day=11))
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 4)
        self.assertEqual([row["date"] for row in result["days"]], ["2026-10-11", "2026-10-10", "2026-10-09"])
        self.assertEqual(result["days"][0]["morning"], "8:00")
        self.assertEqual(result["days"][0]["night"], "")  # 11 号的晚安还没发生
        self.assertEqual(result["days"][0]["sleep_hours"], 8.5)  # 10 号 23:40 → 11 号 8:00（8h20m，半小时步进）
        self.assertEqual(result["days"][1]["night"], "23:40")
        self.assertEqual(result["days"][2]["night"], "23:00")

    def test_report_streaks_anchor_at_latest_event_not_today(self) -> None:
        """今天还没问候，连续不清零（锚定最近一次事件的那天）。"""
        g = _make_greeting(self.root)
        session = private_session("10001")
        for day in (9, 10):
            asyncio.run(g.observe(session, text="早安", now=_at(8, day=day)))
        result = g.report(session.person_id(), now=_at(20, day=11))
        self.assertEqual(result["morning_streak"], 2)
        self.assertEqual(result["night_streak"], 0)

    def test_report_disabled(self) -> None:
        g = _make_greeting(self.root, {"subsystems": {"greeting": False}})
        self.assertFalse(g.report("10001", now=_at(8))["ok"])

    def test_report_respects_days_limit(self) -> None:
        g = _make_greeting(self.root)
        session = private_session("10001")
        for day in (8, 9, 10, 11):
            asyncio.run(g.observe(session, text="早安", now=_at(8, day=day)))
        result = g.report(session.person_id(), days=2, now=_at(9, day=11))
        self.assertEqual([row["date"] for row in result["days"]], ["2026-10-11", "2026-10-10"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
