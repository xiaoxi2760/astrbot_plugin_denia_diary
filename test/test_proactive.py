"""主动消息测试（第 4 步）：闸门 / 暗号四道闸 / 七触发器 / 巡检两段式 / note 预算。

判定全部离线跑（core 不 import astrbot）：真门面 + 真文件，只有 cron 与出站在
``test_main.py`` 里用 stub 验。日期常量的口径：2026-10-04 是周日（走工作日作息表），
``NOW``（21:30）不在任何触发窗里——各触发器测试用自己的时刻。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import TmpDirCase, private_session  # noqa: E402

from core import storage  # noqa: E402
from core.diary.api import Diary  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402
from core.notebook import Notebook, NotebookStore  # noqa: E402
from core.proactive import Proactive, gate, triggers  # noqa: E402
from core.proactive.store import ProactiveStore  # noqa: E402
from core.session import Session  # noqa: E402
from core.state import Affinity, State  # noqa: E402
from core.state.store import AffinityStore, StateStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
SUN = datetime(2026, 10, 4, 21, 30, tzinfo=TZ)  # 周日 21:30（不在任何触发窗）
SUN_NOON = SUN.replace(hour=12, minute=0)  # 劝饭窗
SUN_NIGHT = SUN.replace(hour=22, minute=45)  # 劝睡窗（免打扰 23:30 之前）
MON_MORNING = datetime(2026, 10, 5, 8, 0, tzinfo=TZ)  # 周一 08:00（早安窗 + 日记窗）
MON_GOODNIGHT = datetime(2026, 10, 5, 23, 0, tzinfo=TZ)  # 周一 23:00（晚安窗，免打扰前）
MON_LATE = datetime(2026, 10, 5, 23, 45, tzinfo=TZ)  # 免打扰时段


def run(coro):
    return asyncio.run(coro)


def make_stack(root: Path, config: dict | None = None) -> SimpleNamespace:
    """全套真门面（日记 / 小本本 / 状态 / 熟悉度 / 主动消息），共享一把锁。"""
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    from core import settings as settings_mod

    resolved = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    diary = Diary(
        settings=resolved, layout=layout, store=DiaryStore(layout=layout, locks=locks), locks=locks
    )
    notebook = Notebook(
        settings=resolved,
        layout=layout,
        store=NotebookStore(layout=layout, locks=locks),
        locks=locks,
    )
    state = State(
        settings=resolved, layout=layout, store=StateStore(layout=layout, locks=locks), locks=locks
    )
    affinity = Affinity(
        settings=resolved,
        layout=layout,
        store=AffinityStore(layout=layout, locks=locks),
        locks=locks,
    )
    proactive = Proactive(
        settings=resolved,
        layout=layout,
        store=ProactiveStore(layout=layout, locks=locks),
        locks=locks,
        diary=diary,
        notebook=notebook,
        state=state,
        affinity=affinity,
    )
    return SimpleNamespace(
        settings=resolved,
        layout=layout,
        locks=locks,
        diary=diary,
        notebook=notebook,
        state=state,
        affinity=affinity,
        proactive=proactive,
    )


def seed_contacts(stack: SimpleNamespace, contacts: dict[str, tuple[str, str]]) -> None:
    """ contacts：``{"10001": ("aiocqhttp:FriendMessage:10001", "private")}``"""
    doc = {"contacts": {pid: {"umo": umo, "kind": kind} for pid, (umo, kind) in contacts.items()}}
    storage.atomic_write_json(stack.layout.proactive, doc)
    storage.shared_cache.invalidate(stack.layout.proactive)


def read_proactive(stack: SimpleNamespace) -> dict:
    return storage.read_json(stack.layout.proactive, default={})


def seed_acquaintance(stack: SimpleNamespace, peer: str = "10001", *, last_days_ago: int, now: datetime) -> None:
    """把某人刷到「泛泛」档：连续多天每天三轮私聊（单日封顶 3.0），最后一次互动在
    ``last_days_ago`` 天前。三天 × 3.0 ≈ 9 分，衰减 4 天后仍 ≥ 3（泛泛线）。"""
    session = private_session(peer)
    for offset in range(6, last_days_ago - 1, -1):
        if offset < last_days_ago:
            continue
        moment = now - timedelta(days=offset)
        for minute in (10, 40, 70):
            run(stack.affinity.touch(session, now=moment + timedelta(minutes=minute)))


# ---- 状态表（ProactiveStore） ----------------------------------------------------


class StoreTest(TmpDirCase):
    def test_read_missing_file_returns_empty_doc(self) -> None:
        stack = make_stack(self.root)
        self.assertEqual(stack.proactive.store.read(), {})
        self.assertEqual(stack.proactive.store.sessions(), {})
        self.assertEqual(stack.proactive.store.contacts(), {})

    def test_update_writes_through(self) -> None:
        stack = make_stack(self.root)

        def update(doc):
            doc["sessions"] = {"u1": {"last_sent_at": SUN.isoformat()}}
            return doc

        run(stack.proactive.store.update(update))
        doc = read_proactive(stack)
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("last_sent_at", doc["sessions"]["u1"])

    def test_update_returning_none_writes_nothing(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.store.update(lambda doc: None))
        self.assertFalse(stack.layout.proactive.exists(), "没东西可写就不建文件")

    def test_bad_json_degrades_to_empty(self) -> None:
        stack = make_stack(self.root)
        storage.atomic_write_text(stack.layout.proactive, "{broken")
        self.assertEqual(stack.proactive.store.read(), {})
        self.assertEqual(stack.proactive.store.sessions(), {})

    def test_shape_helpers_ignore_non_dict(self) -> None:
        stack = make_stack(self.root)
        storage.atomic_write_json(stack.layout.proactive, {"sessions": "bad", "contacts": [1]})
        self.assertEqual(stack.proactive.store.sessions(), {})
        self.assertEqual(stack.proactive.store.contacts(), {})


# ---- 联系人记录（note_contact） ---------------------------------------------------


class ContactTest(TmpDirCase):
    def test_note_contact_records_person_mapping(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.note_contact(private_session()))
        contacts = read_proactive(stack)["contacts"]
        self.assertEqual(contacts["10001"]["kind"], "private")
        self.assertEqual(contacts["10001"]["umo"], "aiocqhttp:FriendMessage:10001")

    def test_note_contact_records_group_kind(self) -> None:
        from support import group_session

        stack = make_stack(self.root)
        run(stack.proactive.note_contact(group_session()))
        self.assertEqual(read_proactive(stack)["contacts"]["10001"]["kind"], "group")

    def test_note_contact_skips_write_when_unchanged(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.note_contact(private_session()))
        with mock.patch.object(storage, "atomic_write_json", wraps=storage.atomic_write_json) as spy:
            run(stack.proactive.note_contact(private_session()))
            self.assertEqual(spy.call_count, 0, "值没变不落盘")

    def test_note_contact_swallows_store_errors(self) -> None:
        stack = make_stack(self.root)

        async def boom(update):
            raise RuntimeError("disk full")

        stack.proactive.store.update = boom  # type: ignore[method-assign]
        run(stack.proactive.note_contact(private_session()))  # 不抛即过（复核 #5）

    def test_note_contact_without_person_writes_nothing(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.note_contact(Session(umo="aiocqhttp:GroupMessage:555", group_id="555")))
        self.assertFalse(stack.layout.proactive.exists(), "群里取不到人，不记录")

    # ---- 第 8 步：contacts 落昵称（name 键） --------------------------------------

    def test_note_contact_stores_name_and_keeps_it_when_missing(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.note_contact(private_session(name="阿希")))
        contacts = read_proactive(stack)["contacts"]
        self.assertEqual(contacts["10001"]["name"], "阿希")
        # 空名字（合成 sender / 平台没给）：既不写空串也不擦已有值
        run(stack.proactive.note_contact(private_session(name="")))
        self.assertEqual(read_proactive(stack)["contacts"]["10001"]["name"], "阿希")

    def test_note_contact_overwrites_with_fresher_name(self) -> None:
        stack = make_stack(self.root)
        run(stack.proactive.note_contact(private_session(name="阿希")))
        with mock.patch.object(storage, "atomic_write_json", wraps=storage.atomic_write_json) as spy:
            run(stack.proactive.note_contact(private_session(name="小希")))
            self.assertEqual(spy.call_count, 1, "名字变了要落盘（快路径不许吞掉改名）")
        self.assertEqual(read_proactive(stack)["contacts"]["10001"]["name"], "小希")

    def test_contacts_reader_tolerates_docs_without_name(self) -> None:
        stack = make_stack(self.root)
        storage.atomic_write_json(
            stack.layout.proactive,
            {"contacts": {"20002": {"umo": "aiocqhttp:FriendMessage:20002", "kind": "private"}}},
        )
        contacts = stack.proactive.store.contacts()
        self.assertEqual(contacts["20002"]["umo"], "aiocqhttp:FriendMessage:20002")
        self.assertNotIn("name", contacts["20002"], "老 doc 没有 name 键照常读")


# ---- 闸门（gate_check / signal_gate） ---------------------------------------------


class GateTest(TmpDirCase):
    def make_cfg(self, **over) -> dict:
        from core.settings import _PROACTIVE_DEFAULTS

        cfg = dict(_PROACTIVE_DEFAULTS)
        cfg.update(over)
        return cfg

    def test_clean_entry_passes(self) -> None:
        reason = gate.gate_check(
            entry={}, kind="private", cfg=self.make_cfg(), late_night=False, now=SUN
        )
        self.assertIsNone(reason)

    def test_late_night_blocks(self) -> None:
        reason = gate.gate_check(
            entry={}, kind="private", cfg=self.make_cfg(), late_night=True, now=SUN
        )
        self.assertIsNotNone(reason)

    def test_private_quota_two(self) -> None:
        cfg = self.make_cfg()
        entry = {"today_date": SUN.date().isoformat(), "today_count": 2}
        reason = gate.gate_check(
            entry=entry, kind="private", cfg=cfg, late_night=False, now=SUN
        )
        self.assertIn("配额", str(reason))
        entry["today_count"] = 1
        reason = gate.gate_check(
            entry=entry, kind="private", cfg=cfg, late_night=False, now=SUN
        )
        self.assertIsNone(reason)

    def test_group_quota_one(self) -> None:
        cfg = self.make_cfg()
        entry = {"today_date": SUN.date().isoformat(), "today_count": 1}
        reason = gate.gate_check(entry=entry, kind="group", cfg=cfg, late_night=False, now=SUN)
        self.assertIsNotNone(reason)

    def test_quota_resets_on_date_change(self) -> None:
        cfg = self.make_cfg()
        entry = {"today_date": "2026-10-03", "today_count": 99}
        reason = gate.gate_check(
            entry=entry, kind="private", cfg=cfg, late_night=False, now=SUN
        )
        self.assertIsNone(reason, "today_date 不是今天 → 计数视为 0")

    def test_min_interval_blocks_within_two_hours(self) -> None:
        cfg = self.make_cfg()
        entry = {"last_sent_at": (SUN - timedelta(hours=1)).isoformat()}
        reason = gate.gate_check(
            entry=entry, kind="private", cfg=cfg, late_night=False, now=SUN
        )
        self.assertIsNotNone(reason, "1 小时前发过 → 最小间隔拦截")

    def test_min_interval_passes_after_two_hours(self) -> None:
        cfg = self.make_cfg()
        entry = {"last_sent_at": (SUN - timedelta(hours=2, minutes=1)).isoformat()}
        reason = gate.gate_check(
            entry=entry, kind="private", cfg=cfg, late_night=False, now=SUN
        )
        self.assertIsNone(reason)


class SignalGateTest(TmpDirCase):
    def make_cfg(self, **over) -> dict:
        from core.settings import _PROACTIVE_DEFAULTS

        cfg = dict(_PROACTIVE_DEFAULTS)
        cfg.update(over)
        return cfg

    def test_blocks_person_not_in_love_peers(self) -> None:
        reason = gate.signal_gate(
            person_id="20002",
            love_peers=("10001",),
            snapshot_valence=-2.0,
            entry={},
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIn("名单", str(reason))

    def test_blocks_without_coordinates(self) -> None:
        """从没给过分的期间暗号不触发（宁缺毋滥）。"""
        reason = gate.signal_gate(
            person_id="10001",
            love_peers=("10001",),
            snapshot_valence=None,
            entry={},
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIn("坐标", str(reason))

    def test_blocks_when_valence_above_threshold(self) -> None:
        reason = gate.signal_gate(
            person_id="10001",
            love_peers=("10001",),
            snapshot_valence=-0.5,
            entry={},
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIn("最低档", str(reason))

    def test_blocks_when_already_sent_today(self) -> None:
        entry = {"signal_date": SUN.date().isoformat()}
        reason = gate.signal_gate(
            person_id="10001",
            love_peers=("10001",),
            snapshot_valence=-2.0,
            entry=entry,
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIn("今天", str(reason), "每日一次：今天已发过暗号")

    def test_blocks_during_cooldown(self) -> None:
        entry = {"signal_last_at": (SUN - timedelta(days=1)).isoformat()}
        reason = gate.signal_gate(
            person_id="10001",
            love_peers=("10001",),
            snapshot_valence=-2.0,
            entry=entry,
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIn("冷却", str(reason))

    def test_passes_all_four_gates(self) -> None:
        entry = {"signal_last_at": (SUN - timedelta(days=3, minutes=1)).isoformat()}
        reason = gate.signal_gate(
            person_id="10001",
            love_peers=("10001",),
            snapshot_valence=-1.4,
            entry=entry,
            cfg=self.make_cfg(),
            now=SUN,
        )
        self.assertIsNone(reason)


# ---- 触发器 ---------------------------------------------------------------------


class TriggerCase(TmpDirCase):
    """触发器公共底座：一套门面 + 一个私聊联系人。"""

    def make(self, config: dict | None = None) -> SimpleNamespace:
        stack = make_stack(self.root, config)
        seed_contacts(stack, {"10001": ("aiocqhttp:FriendMessage:10001", "private")})
        return stack

    def peer(self) -> Session:
        return private_session()


class PromiseTriggerTest(TriggerCase):
    def test_due_promise_yields_candidate_with_fragment(self) -> None:
        stack = self.make()
        run(
            stack.notebook.note_add(
                self.peer(), kind="promise", text="周三一起看电影", due_at="2026-10-04", now=SUN
            )
        )
        candidates = triggers.find_promises(stack.proactive, stack.proactive.store.read(), now=SUN)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "promise")
        self.assertEqual(candidates[0].fragment, "周三一起看电影")
        self.assertEqual(candidates[0].umo, "aiocqhttp:FriendMessage:10001")

    def test_future_promise_yields_nothing(self) -> None:
        stack = self.make()
        run(
            stack.notebook.note_add(
                self.peer(), kind="promise", text="下周还书", due_at="2026-10-20", now=SUN
            )
        )
        self.assertEqual(triggers.find_promises(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_promise_without_contact_skipped(self) -> None:
        stack = self.make()
        run(
            stack.notebook.note_add(
                self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN
            )
        )
        seed_contacts(stack, {"20002": ("aiocqhttp:FriendMessage:20002", "private")})
        self.assertEqual(triggers.find_promises(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_promise_already_followed_up_today_skipped(self) -> None:
        stack = self.make()
        run(
            stack.notebook.note_add(
                self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN
            )
        )
        doc = stack.proactive.store.read()
        doc["sessions"] = {
            "aiocqhttp:FriendMessage:10001": {
                "today_date": SUN.date().isoformat(),
                "slots_today": {SUN.date().isoformat(): ["promise"]},
            }
        }
        self.assertEqual(triggers.find_promises(stack.proactive, doc, now=SUN), [])


class AnniversaryTriggerTest(TriggerCase):
    def test_birthday_today_yields_candidate(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="fact", text="他生日是10月4日", now=SUN))
        candidates = triggers.find_anniversaries(stack.proactive, stack.proactive.store.read(), now=SUN)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "anniversary")
        self.assertIn("生日", candidates[0].fragment)

    def test_birthday_other_day_yields_nothing(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="fact", text="他生日是10月12日", now=SUN))
        self.assertEqual(triggers.find_anniversaries(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_date_without_keyword_yields_nothing(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="fact", text="10月4日要交报告", now=SUN))
        self.assertEqual(triggers.find_anniversaries(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_group_contact_yields_nothing(self) -> None:
        """事实只在私聊可见 → 纪念日内容源在群 contact 上取不到。"""
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="fact", text="他生日是10月4日", now=SUN))
        seed_contacts(stack, {"10001": ("aiocqhttp:GroupMessage:555", "group")})
        self.assertEqual(triggers.find_anniversaries(stack.proactive, stack.proactive.store.read(), now=SUN), [])


class CareTriggerTest(TriggerCase):
    def facts(self, stack: SimpleNamespace, text: str = "他经常熬夜到两点") -> None:
        run(stack.notebook.note_add(self.peer(), kind="fact", text=text, now=SUN))

    def test_sleep_care_for_person_talked_today(self) -> None:
        stack = self.make()
        self.facts(stack)
        run(stack.affinity.touch(self.peer(), now=SUN))
        candidates = triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN_NIGHT)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "care")
        self.assertIn("熬夜", candidates[0].fragment)

    def test_sleep_care_skips_person_not_talked_today(self) -> None:
        stack = self.make()
        self.facts(stack)
        seed_acquaintance(stack, last_days_ago=1, now=SUN)  # 最后互动是昨天
        self.assertEqual(triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN_NIGHT), [])

    def test_meal_care_for_person_silent_today(self) -> None:
        stack = self.make()
        self.facts(stack)
        seed_acquaintance(stack, last_days_ago=0, now=SUN - timedelta(days=2))  # 前天聊过，今天没聊
        candidates = triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN_NOON)
        self.assertEqual(len(candidates), 1)
        self.assertIn("饭", candidates[0].instruction)

    def test_meal_care_skips_person_talked_today(self) -> None:
        stack = self.make()
        self.facts(stack)
        run(stack.affinity.touch(self.peer(), now=SUN))
        self.assertEqual(triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN_NOON), [])

    def test_care_outside_window_yields_nothing(self) -> None:
        stack = self.make()
        self.facts(stack)
        run(stack.affinity.touch(self.peer(), now=SUN))
        self.assertEqual(triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_care_needs_facts(self) -> None:
        stack = self.make()
        run(stack.affinity.touch(self.peer(), now=SUN))
        self.assertEqual(
            triggers.find_care(stack.proactive, stack.proactive.store.read(), now=SUN_NIGHT),
            [],
            "小本本里不认识的人不劝（防打卡）",
        )

    def test_care_deduped_same_day(self) -> None:
        stack = self.make()
        self.facts(stack)
        run(stack.affinity.touch(self.peer(), now=SUN))
        doc = stack.proactive.store.read()
        doc["sessions"] = {
            "aiocqhttp:FriendMessage:10001": {
                "today_date": SUN.date().isoformat(),
                "slots_today": {SUN.date().isoformat(): ["care"]},
            }
        }
        self.assertEqual(triggers.find_care(stack.proactive, doc, now=SUN_NIGHT), [])


class QuietTriggerTest(TriggerCase):
    def test_yields_after_quiet_days(self) -> None:
        stack = self.make()
        seed_acquaintance(stack, last_days_ago=4, now=SUN)  # 泛泛 + 4 天没来
        candidates = triggers.find_quiet(stack.proactive, stack.proactive.store.read(), now=SUN)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "quiet")

    def test_skips_recent_contact(self) -> None:
        stack = self.make()
        seed_acquaintance(stack, last_days_ago=1, now=SUN)
        self.assertEqual(triggers.find_quiet(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_skips_strangers(self) -> None:
        stack = make_stack(self.root)
        seed_contacts(stack, {"10001": ("aiocqhttp:FriendMessage:10001", "private")})
        run(stack.affinity.touch(private_session("10001"), now=SUN - timedelta(days=4)))
        self.assertEqual(triggers.find_quiet(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_uses_diary_of_last_day_as_fragment(self) -> None:
        stack = self.make()
        last_day = (SUN - timedelta(days=4)).date().isoformat()
        run(stack.diary.write_async(self.peer(), text="那天你说想学吉他", date=last_day, now=SUN))
        seed_acquaintance(stack, last_days_ago=4, now=SUN)
        candidates = triggers.find_quiet(stack.proactive, stack.proactive.store.read(), now=SUN)
        self.assertEqual(len(candidates), 1)
        self.assertIn("吉他", candidates[0].fragment)

    def test_falls_back_to_time_fact(self) -> None:
        stack = self.make()
        seed_acquaintance(stack, last_days_ago=4, now=SUN)
        candidates = triggers.find_quiet(stack.proactive, stack.proactive.store.read(), now=SUN)
        self.assertEqual(len(candidates), 1)
        self.assertIn("4 天", candidates[0].fragment, "没有日记就用真实的时间事实")


class GreetingTriggerTest(TriggerCase):
    def make(self, config: dict | None = None) -> SimpleNamespace:  # noqa: D102 - 见 TriggerCase
        stack = make_stack(self.root, config)
        seed_contacts(stack, {"10001": ("aiocqhttp:FriendMessage:10001", "private")})
        return stack

    def test_morning_greeting_for_love_peer(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        candidates = triggers.find_greetings(stack.proactive, stack.proactive.store.read(), now=MON_MORNING)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "morning")

    def test_night_greeting_slot(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        candidates = triggers.find_greetings(stack.proactive, stack.proactive.store.read(), now=MON_GOODNIGHT)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "goodnight")

    def test_greeting_skips_when_no_love_peers(self) -> None:
        stack = self.make()
        self.assertEqual(
            triggers.find_greetings(stack.proactive, stack.proactive.store.read(), now=MON_MORNING), []
        )

    def test_greeting_skips_outside_window(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        self.assertEqual(triggers.find_greetings(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_greeting_skips_non_private_contact(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        seed_contacts(stack, {"10001": ("aiocqhttp:GroupMessage:555", "group")})
        self.assertEqual(
            triggers.find_greetings(stack.proactive, stack.proactive.store.read(), now=MON_MORNING), []
        )

    def test_morning_and_night_each_once_per_day(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        doc = stack.proactive.store.read()
        doc["sessions"] = {
            "aiocqhttp:FriendMessage:10001": {
                "today_date": MON_MORNING.date().isoformat(),
                "slots_today": {MON_MORNING.date().isoformat(): ["morning"]},
            }
        }
        self.assertEqual(triggers.find_greetings(stack.proactive, doc, now=MON_MORNING), [])
        night = triggers.find_greetings(stack.proactive, doc, now=MON_GOODNIGHT)
        self.assertEqual(len(night), 1, "早安发过不影响晚安")

    def test_night_window_covers_its_last_minute(self) -> None:
        """第 11 步回归：晚安窗口原来写 ``(23, 0) -> (23, 59)``，配右开区间只到 23:58:59，
        23:59 那一分钟是死区（默认 15 分钟一 tick 碰不到，``patrol_minutes=1`` 就会每天空转）。
        常量改成 ``(24, 0)``（当天结束哨兵）之后必须覆盖到 23:59:59。"""
        last = MON_GOODNIGHT.replace(hour=23, minute=59, second=30)
        self.assertTrue(triggers._in_window(last, triggers.GREETING_NIGHT), "23:59:30 要在窗口内")
        self.assertTrue(triggers._in_window(MON_LATE, triggers.GREETING_NIGHT), "23:45 仍要在窗口内")
        midnight = MON_GOODNIGHT.replace(hour=0, minute=0, second=0)
        self.assertFalse(triggers._in_window(midnight, triggers.GREETING_NIGHT),
                         "右开区间语义不能被改坏：00:00 不在窗口内")
        before = MON_GOODNIGHT.replace(hour=22, minute=59)
        self.assertFalse(triggers._in_window(before, triggers.GREETING_NIGHT), "起点前不在窗口内")


class DiaryTriggerTest(TriggerCase):
    def test_uses_yesterday_entry(self) -> None:
        stack = self.make()
        run(stack.diary.write_async(self.peer(), text="昨天的游乐园人真多", date="2026-10-04", now=SUN))
        candidates = triggers.find_diary_hooks(stack.proactive, stack.proactive.store.read(), now=MON_MORNING)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].slot, "diary")
        self.assertIn("游乐园", candidates[0].fragment)

    def test_silent_outside_window(self) -> None:
        stack = self.make()
        run(stack.diary.write_async(self.peer(), text="昨天的游乐园人真多", date="2026-10-04", now=SUN))
        self.assertEqual(triggers.find_diary_hooks(stack.proactive, stack.proactive.store.read(), now=SUN), [])

    def test_silent_when_no_entry_yesterday(self) -> None:
        stack = self.make()
        self.assertEqual(
            triggers.find_diary_hooks(stack.proactive, stack.proactive.store.read(), now=MON_MORNING), []
        )


# ---- 巡检决策（patrol：先闸门后内容 / 一 tick 一条 / 两段式） ------------------------


class PatrolTest(TriggerCase):
    def signal_ready(self, stack: SimpleNamespace, *, now: datetime) -> None:
        run(stack.state.observe(self.peer(), valence=-2, now=now))

    def test_disabled_returns_none(self) -> None:
        stack = make_stack(self.root, {"subsystems": {"proactive": False}})
        seed_contacts(stack, {"10001": ("aiocqhttp:FriendMessage:10001", "private")})
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)))

    def test_no_candidate_returns_none(self) -> None:
        stack = self.make()
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)))

    def test_wake_decision_for_due_promise(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        self.assertEqual(decision["kind"], "wake")
        self.assertEqual(decision["slot"], "promise")
        self.assertEqual(decision["umo"], "aiocqhttp:FriendMessage:10001")
        self.assertIn("周三考试", decision["note"])

    def test_signal_beats_promise(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        self.signal_ready(stack, now=SUN)
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        self.assertEqual(decision["kind"], "direct")
        self.assertEqual(decision["slot"], "signal")
        self.assertEqual(decision["char"], "。")

    def test_two_phase_quota_deducted_without_last_sent_at(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        entry = read_proactive(stack)["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertEqual(entry["today_count"], 1)
        self.assertEqual(entry["last_slot"], "promise")
        self.assertNotIn("last_sent_at", entry, "决策时绝不写 last_sent_at（两段式第一步）")

    def test_confirm_sent_writes_last_sent_at(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN + timedelta(minutes=5)))
        entry = read_proactive(stack)["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertEqual(entry["last_sent_at"], (SUN + timedelta(minutes=5)).isoformat(timespec="seconds"))

    def test_one_tick_one_message(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        first = run(stack.proactive.patrol(now=SUN))
        self.assertIsNotNone(first)
        second = run(stack.proactive.patrol(now=SUN + timedelta(minutes=1)))
        self.assertIsNone(second, "同一天同 slot 已发（slots_today 防重），本 tick 没有别的可说")

    def test_quota_exhausted_blocks_next_day_wake(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        # 私聊配额 2：第二天还能发（today_date 翻转），但同一天第二条 promise 被 slots_today 挡
        decision = run(stack.proactive.patrol(now=SUN + timedelta(hours=3)))
        self.assertIsNone(decision)

    def test_late_night_blocks_wake(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-05", now=SUN))
        self.assertIsNone(run(stack.proactive.patrol(now=MON_LATE)), "免打扰时段常规内容全静默")

    def test_signal_breaks_through_late_night(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        self.signal_ready(stack, now=MON_LATE)
        decision = run(stack.proactive.patrol(now=MON_LATE))
        assert decision is not None
        self.assertEqual(decision["kind"], "direct", "暗号破免打扰（决策 #16）")

    def test_signal_penetrates_full_quota_and_late_night(self) -> None:
        """验收裁定：暗号穿透配额——配额扣满 + 深夜 + 最低档自报 → 仍产出 direct。"""
        stack = self.make({"love_peers": ["10001"]})
        self.signal_ready(stack, now=MON_LATE)

        def drain(doc):
            doc["sessions"] = {
                "aiocqhttp:FriendMessage:10001": {
                    "today_date": MON_LATE.date().isoformat(),
                    "today_count": 2,
                }
            }
            return doc

        run(stack.proactive.store.update(drain))
        decision = run(stack.proactive.patrol(now=MON_LATE))
        assert decision is not None
        self.assertEqual(decision["kind"], "direct")
        entry = read_proactive(stack)["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertEqual(entry["today_count"], 3, "穿透配额但 today_count 照常自增")

    def test_regular_wake_still_blocked_by_full_quota(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))

        def drain(doc):
            doc["sessions"] = {
                "aiocqhttp:FriendMessage:10001": {
                    "today_date": SUN.date().isoformat(),
                    "today_count": 2,
                }
            }
            return doc

        run(stack.proactive.store.update(drain))
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)), "常规内容配额满照挡（只有暗号穿透）")

    def test_signal_deducts_signal_fields(self) -> None:
        """第 11 步改判：决策时**只预留**（配额 / slots_today / last_slot），
        暗号的冷却字段要等**真的发出去了**（`confirm_sent`）才写。

        改之前 `signal_date` / `signal_last_at` 记在 `_commit`：发送失败不回滚，
        于是"一次没送达 = 她 3 天不能再用暗号"，而她本人和用户都不知道为什么。
        """
        stack = self.make({"love_peers": ["10001"]})
        self.signal_ready(stack, now=SUN)
        run(stack.proactive.patrol(now=SUN))
        entry = read_proactive(stack)["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertEqual(entry["last_slot"], "signal")
        self.assertNotIn("signal_date", entry, "还没发出去，冷却一个字都不能写")
        self.assertNotIn("signal_last_at", entry, "还没发出去，冷却一个字都不能写")
        self.assertNotIn("last_sent_at", entry, "直发也要等确认点才写时间戳")

        # 确认送达 → 冷却与每日一次这时候才落下去
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        entry = read_proactive(stack)["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertEqual(entry["signal_date"], SUN.date().isoformat())
        self.assertEqual(entry["signal_last_at"], SUN.isoformat(timespec="seconds"))
        self.assertEqual(entry["last_sent_at"], SUN.isoformat(timespec="seconds"))

    def test_signal_send_failure_does_not_burn_cooldown(self) -> None:
        """第 11 步回归（这条就是那个 bug）：发失败 = 没调 `confirm_sent`，
        下一次巡检必须还能再发——冷却**不能**被烧掉。"""
        stack = self.make({"love_peers": ["10001"]})
        self.signal_ready(stack, now=SUN)
        first = run(stack.proactive.patrol(now=SUN))
        self.assertIsNotNone(first, "第一次决策先出来")
        # 模拟"平台没送达"：拿到决策但不确认。minutes 之间也足够越过最小间隔。
        again = run(stack.proactive.patrol(now=SUN + timedelta(minutes=10)))
        self.assertIsNotNone(again, "上次没送达 → 这次还得能发（冷却没被烧）")
        self.assertEqual(again["kind"], "direct")
        # 真送达之后才该被拦住
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN + timedelta(minutes=10)))
        blocked = run(stack.proactive.patrol(now=SUN + timedelta(minutes=20)))
        self.assertIsNone(blocked, "送达之后：今天已发过 + 冷却中，必须拦住")

    def test_signal_not_sent_to_non_contact(self) -> None:
        stack = make_stack(self.root, {"love_peers": ["10001"]})  # 没记过 contact
        self.signal_ready(stack, now=SUN)
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)), "不知道往哪发（没有 umo）就发不出")


class NoteTest(TriggerCase):
    def test_note_is_inject_plus_pack(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.state.observe(self.peer(), word="有点烦", valence=-1, now=SUN))
        run(stack.affinity.touch(self.peer(), now=SUN))
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        note = decision["note"]
        self.assertIn("【约定跟进】", note)
        self.assertIn("周三考试", note)
        self.assertIn("素材：", note)
        self.assertGreater(len(note), len(f"【约定跟进】素材：周三考试"), "注入本体也在 note 里（人格一致）")

    def test_pack_capped_at_100_chars(self) -> None:
        stack = self.make()
        run(
            stack.notebook.note_add(
                self.peer(), kind="promise", text="很长" * 40, due_at="2026-10-04", now=SUN
            )
        )
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        pack = decision["note"].rsplit("\n", 1)[-1]
        self.assertLessEqual(len(pack), 100)
        self.assertTrue(pack.startswith("【约定跟进】"))

    def test_pack_has_no_newlines(self) -> None:
        """note 经宿主 json.dumps 转义，pack 里不放换行（预算按转义后算，复核 #6）。"""
        stack = self.make()
        last_day = (SUN - timedelta(days=4)).date().isoformat()
        run(stack.diary.write_async(self.peer(), text="第一段\n第二段\n第三段", date=last_day, now=SUN))
        seed_acquaintance(stack, last_days_ago=4, now=SUN)
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        pack = decision["note"].rsplit("\n", 1)[-1]
        self.assertNotIn("\n", pack)


class ProactiveLogTest(TriggerCase):
    """发送记录 proactive-log.jsonl（验收 P.S. #11）：确认发出后追加，200 行 / 90 天。"""

    def seed_log(self, stack: SimpleNamespace, lines: list[dict]) -> None:
        body = "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines)
        storage.atomic_write_text(stack.layout.proactive_log, body)
        storage.shared_cache.invalidate(stack.layout.proactive_log)

    def log_lines(self, stack: SimpleNamespace) -> list[dict]:
        return stack.proactive.store.read_log_lines()

    def test_confirm_appends_log_line(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN + timedelta(minutes=5)))
        lines = self.log_lines(stack)
        self.assertEqual(len(lines), 1)
        self.assertEqual(set(lines[0]), {"ts", "umo", "slot", "fragment"})
        self.assertEqual(lines[0]["slot"], "promise")
        self.assertEqual(lines[0]["fragment"], "周三考试")
        self.assertEqual(lines[0]["umo"], "aiocqhttp:FriendMessage:10001")
        self.assertEqual(lines[0]["ts"], (SUN + timedelta(minutes=5)).isoformat(timespec="seconds"))

    def test_decision_without_confirmation_logs_nothing(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        self.assertFalse(stack.layout.proactive_log.exists(), "决策不记，确认发出才记")

    def test_signal_confirm_logs_signal_slot(self) -> None:
        stack = self.make({"love_peers": ["10001"]})
        run(stack.state.observe(self.peer(), valence=-2, now=SUN))
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        lines = self.log_lines(stack)
        self.assertEqual(lines[0]["slot"], "signal")
        self.assertIn("暗号", lines[0]["fragment"])

    def test_confirm_without_pending_uses_last_slot(self) -> None:
        """重启丢失决策暂存：确认仍记行，slot 回落 last_slot、fragment 记空串。"""
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        run(stack.proactive.patrol(now=SUN))
        stack.proactive._pending_log.clear()  # 模拟重启
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        lines = self.log_lines(stack)
        self.assertEqual(lines[0]["slot"], "promise")
        self.assertEqual(lines[0]["fragment"], "")

    def test_log_pruned_to_200_lines(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        old = [
            {"ts": (SUN - timedelta(minutes=i)).isoformat(timespec="seconds"),
             "umo": "u", "slot": "quiet", "fragment": f"旧{i}"}
            for i in range(200)
        ]
        self.seed_log(stack, old)
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        lines = self.log_lines(stack)
        self.assertEqual(len(lines), 200)
        self.assertEqual(lines[-1]["slot"], "promise", "最新一条在末尾")

    def test_log_prunes_lines_older_than_90_days(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        self.seed_log(stack, [
            {"ts": (SUN - timedelta(days=91)).isoformat(timespec="seconds"),
             "umo": "u", "slot": "quiet", "fragment": "太老"},
            {"ts": (SUN - timedelta(days=1)).isoformat(timespec="seconds"),
             "umo": "u", "slot": "quiet", "fragment": "还新"},
        ])
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        lines = self.log_lines(stack)
        self.assertEqual([line["fragment"] for line in lines], ["还新", "周三考试"], "91 天前的旧行被裁")

    def test_log_skips_malformed_lines(self) -> None:
        stack = self.make()
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        storage.atomic_write_text(
            stack.layout.proactive_log,
            "这不是json\n{\"ts\":\"不是时间\",\"umo\":\"u\"}\n",
        )
        run(stack.proactive.patrol(now=SUN))
        run(stack.proactive.confirm_sent("aiocqhttp:FriendMessage:10001", now=SUN))
        lines = self.log_lines(stack)
        self.assertEqual(len(lines), 1, "坏行丢弃，只留新确认的行")


# ---- 启用范围（第 7 步）：目标宇宙走 scope.proactive_targets -------------------------


class ScopeTargetsPatrolTest(TriggerCase):
    """三档下 patrol 的目标集合：群候选只在 all 档出现，且受 daily_limit_group 与 @ 护栏。"""

    GROUP_UMO = "aiocqhttp:GroupMessage:555"

    def seed_group_promise(self, stack: SimpleNamespace) -> None:
        seed_contacts(stack, {"10001": (self.GROUP_UMO, "group")})
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))

    def write_sessions(self, stack: SimpleNamespace, sessions: dict) -> None:
        doc = read_proactive(stack)
        doc["sessions"] = sessions
        storage.atomic_write_json(stack.layout.proactive, doc)

    def test_all_mode_yields_group_candidate_with_at_rule(self) -> None:
        stack = self.make({"scope": {"mode": "all"}, "love_peers": ["10001"]})
        self.seed_group_promise(stack)
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        self.assertEqual(decision["kind"], "wake")
        self.assertEqual(decision["umo"], self.GROUP_UMO)
        self.assertIn("@", decision["note"], "群里不许裸发：note 必须要求 @ 对方")

    def test_group_quota_is_daily_limit_group(self) -> None:
        stack = self.make({"scope": {"mode": "all"}, "love_peers": ["10001"]})
        self.seed_group_promise(stack)
        self.write_sessions(stack, {
            self.GROUP_UMO: {"today_date": SUN.date().isoformat(), "today_count": 1},
        })
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)), "群配额默认 1 条/天，已满就不发")

    def test_private_mode_never_targets_groups(self) -> None:
        stack = self.make({"love_peers": ["10001"]})  # 默认档 private
        self.seed_group_promise(stack)
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)))

    def test_owner_mode_excludes_non_master_private_contacts(self) -> None:
        stack = self.make({"scope": {"mode": "owner"}, "love_peers": ["10001"]})
        run(stack.notebook.note_add(self.peer(), kind="promise", text="周三考试", due_at="2026-10-04", now=SUN))
        seed_contacts(stack, {"20002": ("aiocqhttp:FriendMessage:20002", "private")})
        self.assertIsNone(run(stack.proactive.patrol(now=SUN)), "owner 档只对主人主动")
        seed_contacts(stack, {"10001": ("aiocqhttp:FriendMessage:10001", "private")})
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        self.assertEqual(decision["umo"], "aiocqhttp:FriendMessage:10001")

    def test_quota_is_counted_per_session(self) -> None:
        """同一个 private 档：A 发满不影响 B（配额按会话，不按档位总量）。"""
        stack = self.make({"scope": {"mode": "private"}, "love_peers": ["10001"]})
        run(stack.notebook.note_add(self.peer(), kind="promise", text="帮 A 带书", due_at="2026-10-04", now=SUN))
        run(stack.notebook.note_add(
            self.peer(), kind="promise", text="帮 B 还钱", about="20002", due_at="2026-10-04", now=SUN
        ))
        seed_contacts(stack, {"20002": ("aiocqhttp:FriendMessage:20002", "private")})
        full = SUN.date().isoformat()
        self.write_sessions(stack, {
            "aiocqhttp:FriendMessage:10001": {"today_date": full, "today_count": 2},
        })
        decision = run(stack.proactive.patrol(now=SUN))
        assert decision is not None
        self.assertEqual(decision["umo"], "aiocqhttp:FriendMessage:20002", "A 满了，轮到 B")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
