"""群社交测试（任务 1）：登记 / 有界话题 / bot 短聊闸门 / 群巡检候选 / 注入预算。

全部离线跑（core 不 import astrbot）：真门面 + 真文件 + 假时钟，只有宿主钩子
（``event_message_type`` 装饰器、``stop_event``）不在覆盖范围——那部分在
``main.py`` 里只有一层薄壳，实体 ``_social_observe`` 的判定全部收在 core.social，
这里的测试直接打门面。

隐私红线（任务书验收）在本文件钉住：

- 私聊恋爱日记的独特字符串不出现在群 note / 注入 / 候选里；
- A 群的话题不进 B 群的提示词；私聊注入里永远没有"群里最近"。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import TmpDirCase, private_session, group_session  # noqa: E402

from core import compose, scope, storage  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core.diary.api import Diary  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402
from core.life import Life  # noqa: E402
from core.notebook import Notebook, NotebookStore  # noqa: E402
from core.proactive import Proactive, gate  # noqa: E402
from core.proactive.store import ProactiveStore  # noqa: E402
from core.social import Social  # noqa: E402
from core.social import chat, registry, topics  # noqa: E402
from core.state import Affinity, State  # noqa: E402
from core.state.store import AffinityStore, StateStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOON = datetime(2026, 10, 26, 14, 0, tzinfo=TZ)  # 周一 14:00，不在任何触发窗
GROUP = "aiocqhttp:GroupMessage:555"
GROUP_B = "aiocqhttp:GroupMessage:666"


def run(coro):
    return asyncio.run(coro)


def make_stack(root: Path, config: dict | None = None) -> SimpleNamespace:
    """全套真门面（日记 / 小本本 / 状态 / 熟悉度 / 生活 / 群社交 / 主动消息），共享一把锁。"""
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    resolved = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    diary = Diary(
        settings=resolved, layout=layout, store=DiaryStore(layout=layout, locks=locks), locks=locks
    )
    notebook = Notebook(
        settings=resolved, layout=layout, store=NotebookStore(layout=layout, locks=locks), locks=locks
    )
    state = State(
        settings=resolved, layout=layout, store=StateStore(layout=layout, locks=locks), locks=locks
    )
    affinity = Affinity(
        settings=resolved, layout=layout, store=AffinityStore(layout=layout, locks=locks), locks=locks
    )
    life = Life(settings=resolved, layout=layout, locks=locks)
    social = Social(settings=resolved, layout=layout, locks=locks, life=life)
    proactive = Proactive(
        settings=resolved, layout=layout, store=ProactiveStore(layout=layout, locks=locks),
        locks=locks, diary=diary, notebook=notebook, state=state, affinity=affinity,
        life=life, social=social,
    )
    return SimpleNamespace(
        settings=resolved, layout=layout, locks=locks, diary=diary, notebook=notebook,
        state=state, affinity=affinity, life=life, social=social, proactive=proactive,
    )


async def seed_enabled_group(stack: SimpleNamespace, umo: str = GROUP, *, enabled: bool = True) -> None:
    parts = umo.split(":")
    await stack.social.save_group(
        {"platform": parts[0], "group_id": parts[2], "name": "测试群"}, now=NOON
    )
    if enabled:
        await stack.social.save_group({"umo": umo, "enabled": True}, now=NOON)


async def seed_bot(
    stack: SimpleNamespace, umo: str = GROUP, pid: str = "bot1",
    *, is_bot: bool = True, allow: bool = True, name: str = "小助手",
) -> None:
    await stack.social.save_participant(
        umo, {"id": pid, "name": name, "is_bot": is_bot, "allow_interaction": allow}, now=NOON
    )


# ---- 登记 ------------------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_group_identity_is_platform_plus_real_id(self) -> None:
        record, reason = registry.normalize_group(
            {"platform": "aiocqhttp", "group_id": "555", "name": "测试群"},
            now="2026-10-26T14:00:00",
        )
        self.assertFalse(reason)
        self.assertEqual(record["umo"], "aiocqhttp:GroupMessage:555")
        self.assertEqual(record["group_id"], "555")
        self.assertFalse(record["enabled"])  # 新登记默认不主动发言

    def test_group_requires_real_id(self) -> None:
        record, reason = registry.normalize_group({"name": "只有名字"}, now="x")
        self.assertIsNone(record)
        self.assertTrue(reason)
        record, reason = registry.normalize_group(
            {"platform": "aiocqhttp", "group_id": "5 55"}, now="x"
        )
        self.assertIsNone(record)  # id 里有空白：不像真实 id，拒绝而不猜

    def test_group_update_keeps_unset_fields(self) -> None:
        first, _ = registry.normalize_group(
            {"platform": "aiocqhttp", "group_id": "555", "name": "测试群"}, now="t1"
        )
        second, _ = registry.normalize_group({"umo": first["umo"], "enabled": True}, now="t2", existing=first)
        self.assertTrue(second["enabled"])
        self.assertEqual(second["name"], "测试群")  # 只改开关不动名字
        self.assertEqual(second["registered_at"], "t1")

    def test_participant_identity_by_id_not_nickname(self) -> None:
        record, _ = registry.normalize_participant(
            {"id": "10086", "name": "同名的人"}, now="t"
        )
        self.assertEqual(record["id"], "10086")
        self.assertFalse(record["is_bot"])
        self.assertFalse(record["allow_interaction"])
        # 没给 id、只给名字：拒绝（身份不按昵称猜）
        record, reason = registry.normalize_participant({"name": "某人"}, now="t")
        self.assertIsNone(record)
        self.assertTrue(reason)

    def test_participant_bot_flag_only_from_master(self) -> None:
        existing, _ = registry.normalize_participant({"id": "b1"}, now="t1")
        # 自动补录路径不带 is_bot：维持"人"（False），绝不猜成 bot
        auto, _ = registry.normalize_participant({"id": "b1", "name": "新名字"}, now="t2", existing=existing)
        self.assertFalse(auto["is_bot"])
        self.assertEqual(auto["name"], "新名字")  # 显示名可以更新
        # 主人标注才是唯一来源
        marked, _ = registry.normalize_participant({"id": "b1", "is_bot": True}, now="t3", existing=existing)
        self.assertTrue(marked["is_bot"])


# ---- 有界话题 --------------------------------------------------------------------


class TopicsTests(unittest.TestCase):
    def test_record_is_bounded_and_truncated(self) -> None:
        items: list[dict] = []
        for i in range(20):
            items = topics.record_message(
                items, ts=f"2026-10-26T14:{i:02d}:00", speaker_id=f"p{i}",
                speaker_name=f"p{i}", text="很长的消息" * 60, max_items=12, max_chars=120,
            )
        self.assertEqual(len(items), 12)
        self.assertTrue(all(len(item["text"]) <= 120 for item in items))
        self.assertEqual(items[0]["speaker_id"], "p8")  # 最旧的被挤掉

    def test_same_text_dedups(self) -> None:
        first = topics.record_message(
            [], ts="t1", speaker_id="p1", speaker_name="a", text="你好", max_items=5, max_chars=10
        )
        again = topics.record_message(
            first, ts="t2", speaker_id="p1", speaker_name="a", text="你好", max_items=5, max_chars=10
        )
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["ts"], "t1")  # 刷屏不刷新记录

    def test_ttl_expiry_filters_reading(self) -> None:
        items = topics.record_message(
            [], ts=(NOON - timedelta(hours=30)).isoformat(timespec="seconds"),
            speaker_id="p1", speaker_name="a", text="旧话题", max_items=5, max_chars=10,
        )
        items += topics.record_message(
            items, ts=NOON.isoformat(timespec="seconds"),
            speaker_id="p2", speaker_name="b", text="新话题", max_items=5, max_chars=10,
        )
        fresh = topics.fresh_items(items, now=NOON, ttl_hours=24)
        self.assertEqual([item["text"] for item in fresh], ["新话题"])
        # 候选素材取最新一条新鲜话题
        self.assertEqual(topics.material_from(fresh)["text"], "新话题")
        self.assertEqual(topics.material_from(fresh)["speaker_id"], "p2")

    def test_trace_line_has_source_time_and_budget(self) -> None:
        items = topics.record_message(
            [], ts=NOON.isoformat(timespec="seconds"), speaker_id="p1",
            speaker_name="小明", text="去跑步吗", max_items=5, max_chars=10,
        )
        line = topics.trace_line_text(items, now=NOON, budget=70)
        self.assertIn("14:00", line)
        self.assertIn("小明", line)
        self.assertIn("去跑步吗", line)
        self.assertLessEqual(len(line), 70)
        self.assertEqual(topics.trace_line_text([], now=NOON, budget=70), "")


# ---- 观察入口 --------------------------------------------------------------------


class ObserveTests(TmpDirCase):
    def test_scope_private_records_nothing(self) -> None:
        stack = make_stack(self.root)  # 默认 scope=private
        session = group_session(group_id="555")
        outcome = run(stack.social.observe_group(session, text="大家好", now=NOON))
        self.assertFalse(outcome["observed"])
        doc = stack.social.store.read()
        self.assertEqual(doc, {})  # 一个字节都没写

    def test_scope_owner_denies_groups_too(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "owner"}})
        outcome = run(stack.social.observe_group(group_session(), text="嗨", now=NOON))
        self.assertFalse(outcome["observed"])
        self.assertEqual(stack.social.store.read(), {})

    def test_unregistered_group_lands_in_seen_only(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "all"}})
        outcome = run(stack.social.observe_group(group_session(group_id="555"), text="大家好", now=NOON))
        self.assertFalse(outcome["observed"])
        doc = stack.social.store.read()
        self.assertIn(GROUP, doc["seen"])
        self.assertNotIn("topics", doc)  # 没登记的群不攒话题
        self.assertNotIn("groups", doc)

    def test_disabled_group_records_nothing_after_registration(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "all"}})
        run(seed_enabled_group(stack, enabled=False))
        run(stack.social.observe_group(group_session(group_id="555"), text="大家好", now=NOON))
        doc = stack.social.store.read()
        self.assertNotIn("topics", doc)
        self.assertNotIn("participants", doc)

    def test_enabled_group_records_topic_and_auto_participant(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "all"}})
        run(seed_enabled_group(stack))
        session = group_session(group_id="555", sender="10086", sender_name="小明")
        outcome = run(stack.social.observe_group(session, text="下午一起去跑步吗", now=NOON))
        self.assertTrue(outcome["observed"])
        doc = stack.social.store.read()
        self.assertEqual(doc["topics"][GROUP][0]["text"], "下午一起去跑步吗")
        participant = doc["participants"][GROUP]["10086"]
        self.assertEqual(participant["name"], "小明")  # 显示名照实落
        self.assertFalse(participant["is_bot"])  # 是不是 bot 只能主人标
        self.assertFalse(participant["allow_interaction"])

    def test_seen_is_bounded(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "all"}, "social": {"seen_max": 3}})
        for gid in range(10):
            run(stack.social.observe_group(group_session(group_id=str(gid)), text="嗨", now=NOON))
        seen = stack.social.store.read()["seen"]
        self.assertLessEqual(len(seen), 3)


# ---- bot 短聊闸门 ----------------------------------------------------------------


class ChatGateTests(TmpDirCase):
    def make(self, config: dict | None = None) -> SimpleNamespace:
        stack = make_stack(self.root, {
            "scope": {"mode": "all"},
            "social": {"self_names": "小澄、澄澄", "session_cooldown_minutes": 0, **(config or {})},
        })
        run(seed_enabled_group(stack))
        run(seed_bot(stack))
        return stack

    def bot(self, pid: str = "bot1", name: str = "小助手") -> Session:
        return group_session(group_id="555", sender=pid, sender_name=name)

    def test_only_registered_bots_can_start(self) -> None:
        stack = self.make()
        stranger = run(stack.social.chat_decision(self.bot("ghost"), text="小澄，在吗", now=NOON))
        self.assertEqual(stranger["action"], "ignore")
        self.assertIn("没有登记", stranger["reason"])
        run(seed_bot(stack, pid="human", is_bot=False, allow=True))
        human = run(stack.social.chat_decision(self.bot("human"), text="小澄，在吗", now=NOON))
        self.assertIn("只与登记为 bot", human["reason"])
        run(seed_bot(stack, pid="quiet_bot", allow=False))
        quiet = run(stack.social.chat_decision(self.bot("quiet_bot"), text="小澄，在吗", now=NOON))
        self.assertIn("没有允许", quiet["reason"])
        self.assertNotIn("chats", stack.social.store.read())  # 全都没落状态

    def test_self_names_gate(self) -> None:
        stack = self.make({"self_names": ""})
        decision = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("称呼", decision["reason"])
        stack2 = self.make()
        decision = run(stack2.social.chat_decision(self.bot(), text="有人吗", now=NOON))
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("没有叫她", decision["reason"])

    def test_late_night_blocks_without_state_change(self) -> None:
        stack = self.make()
        decision = run(
            stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON, late_night=True)
        )
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("免打扰", decision["reason"])
        # 深夜没落指纹：明早同一条消息可以正常开场
        decision = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
        self.assertEqual(decision["action"], "wake")

    def test_three_rounds_then_stop(self) -> None:
        stack = self.make({"session_cooldown_minutes": 30})
        decisions = [
            run(stack.social.chat_decision(self.bot(), text=f"小澄，问题{i}", now=NOON))
            for i in range(4)
        ]
        self.assertEqual(
            [(item["action"], item["round"]) for item in decisions],
            [("wake", 1), ("wake", 2), ("wake", 3), ("ignore", 0)],
        )
        self.assertIn("3 轮上限", decisions[3]["reason"])
        self.assertIn("自己停下", decisions[3]["reason"])
        # 会话已关、进入冷却
        entry = stack.social.store.read()["chats"][GROUP]
        self.assertIsNone(entry["active"])
        self.assertTrue(entry["cooldown_until"])
        # 封口那条消息已记指纹：原样重放不能再唤起
        replay = run(stack.social.chat_decision(self.bot(), text="小澄，问题3", now=NOON + timedelta(minutes=40)))
        self.assertEqual(replay["action"], "ignore")

    def test_replayed_message_cannot_reawaken(self) -> None:
        stack = self.make()
        first = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
        self.assertEqual(first["action"], "wake")
        for _ in range(5):
            replay = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
            self.assertEqual(replay["action"], "ignore")
            self.assertIn("重复消息", replay["reason"])
        # 到了轮数上限、冷却之后再重放同一段：仍然被去重拦住
        late = NOON + timedelta(hours=2)
        replay = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=late))
        self.assertEqual(replay["action"], "ignore")
        self.assertIn("重复消息", replay["reason"])

    def test_session_cooldown_then_new_session(self) -> None:
        stack = self.make({"max_rounds": 1, "session_cooldown_minutes": 30})
        first = run(stack.social.chat_decision(self.bot(), text="小澄，问题0", now=NOON))
        self.assertEqual(first["action"], "wake")
        # 1 轮上限：第二条消息自己停下并进入冷却
        closed = run(stack.social.chat_decision(self.bot(), text="小澄，问题1", now=NOON + timedelta(minutes=1)))
        self.assertIn("轮上限", closed["reason"])
        blocked = run(
            stack.social.chat_decision(self.bot(), text="小澄，新话头", now=NOON + timedelta(minutes=5))
        )
        self.assertIn("冷却", blocked["reason"])
        after = run(
            stack.social.chat_decision(
                self.bot(), text="小澄，新话头", now=NOON + timedelta(minutes=36)
            )
        )
        self.assertEqual((after["action"], after["round"]), ("wake", 1))

    def test_window_expiry_closes_session(self) -> None:
        stack = self.make()
        run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
        # 10 分钟窗口过期（默认 10），31 分钟后回来：旧会话关闭，冷却 0 → 直接开新会话
        later = run(
            stack.social.chat_decision(self.bot(), text="小澄，还在吗", now=NOON + timedelta(minutes=31))
        )
        self.assertEqual((later["action"], later["round"]), ("wake", 1))
        self.assertEqual(later["reason"], "第 1/3 轮")

    def test_second_bot_does_not_hijack_active_session(self) -> None:
        stack = self.make()
        run(seed_bot(stack, pid="bot2", name="二号机"))
        first = run(stack.social.chat_decision(self.bot(), text="小澄，在吗", now=NOON))
        self.assertEqual(first["action"], "wake")
        second = run(
            stack.social.chat_decision(self.bot("bot2", "二号机"), text="小澄，我先说", now=NOON)
        )
        self.assertEqual(second["action"], "ignore")
        self.assertIn("别的 bot", second["reason"])

    def test_same_bot_in_two_groups_is_independent(self) -> None:
        stack = self.make()
        run(seed_enabled_group(stack, GROUP_B))
        run(seed_bot(stack, GROUP_B, pid="bot1"))
        a = run(stack.social.chat_decision(self.bot(), text="小澄，A 群的事", now=NOON))
        b = run(
            stack.social.chat_decision(
                group_session(group_id="666", sender="bot1"), text="小澄，B 群的事", now=NOON
            )
        )
        self.assertEqual((a["action"], a["round"]), ("wake", 1))
        self.assertEqual((b["action"], b["round"]), ("wake", 1))  # 各群各的会话，不串轮数
        chats = stack.social.store.read()["chats"]
        self.assertIn(GROUP, chats)
        self.assertIn(GROUP_B, chats)

    def test_concurrent_and_out_of_order_events_stay_consistent(self) -> None:
        stack = self.make()

        async def blast() -> list[dict]:
            async def one(text: str) -> dict:
                return await stack.social.chat_decision(self.bot(), text=text, now=NOON)

            same = "小澄，同一句话"
            results = await asyncio.gather(
                one(same), one(same),  # 重放
                one("小澄，第二句"), one("小澄，第三句"),  # 并发推进
            )
            return results

        results = run(blast())
        wakes = [item for item in results if item["action"] == "wake"]
        self.assertLessEqual(len(wakes), 3)
        entry = stack.social.store.read()["chats"][GROUP]
        rounds = int((entry.get("active") or {}).get("rounds") or 0)
        self.assertLessEqual(rounds, 3)
        # 会话轮数与唤醒次数一致：没有重复计轮、没有超过上限
        self.assertEqual(rounds, len(wakes))

    def test_decisions_log_is_bounded_and_readable(self) -> None:
        stack = self.make({"max_rounds": 1, "session_cooldown_minutes": 0})
        for i in range(60):
            run(stack.social.chat_decision(self.bot(), text=f"小澄，问题{i}", now=NOON))
        decisions = stack.social.store.read()["decisions"]
        self.assertLessEqual(len(decisions), 50)
        self.assertTrue(all("reason" in item for item in decisions))
        self.assertIn("wake", {item["action"] for item in decisions})
        self.assertIn("ignore", {item["action"] for item in decisions})


def stack_social_reason(stack: SimpleNamespace) -> str:
    decisions = stack.social.store.read().get("decisions") or []
    for item in reversed(decisions):
        if item.get("action") == "ignore":
            return str(item.get("reason") or "")
    return ""# ---- 巡检群候选（接话题 / 分享日程） -----------------------------------------------


class PatrolGroupTests(TmpDirCase):
    def make(self, config: dict | None = None) -> SimpleNamespace:
        stack = make_stack(self.root, {
            "scope": {"mode": "all"},
            "social": {"self_names": "小澄"},
            **(config or {}),
        })
        run(seed_enabled_group(stack))
        return stack

    def speak(self, stack: SimpleNamespace, text: str = "晚上一起去看流星雨吗") -> None:
        run(stack.social.observe_group(
            group_session(group_id="555", sender="10086", sender_name="小明"), text=text, now=NOON
        ))

    def test_topic_candidate_in_patrol(self) -> None:
        stack = self.make()
        self.speak(stack)
        decision = run(stack.proactive.patrol(now=NOON))
        self.assertIsNotNone(decision)
        self.assertEqual(decision["kind"], "wake")
        self.assertEqual(decision["slot"], "group_topic")
        self.assertIn("流星雨", decision["fragment"])
        self.assertIn("不猜测收件人", decision["note"])
        self.assertIn("不 @ 全体", decision["note"])
        self.assertNotIn("必须先 @ TA", decision["note"])
        # 两段式第一步：配额已扣，last_sent_at 还没写（没确认就当没发出）
        entry = gate.entry_of(stack.proactive.store.read(), GROUP)
        self.assertEqual(entry.get("today_count"), 1)
        self.assertNotIn("last_sent_at", entry)
        self.assertEqual(stack.proactive.store.read_log_lines(), [])
        # 第二步：确认后写轨迹
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        lines = stack.proactive.store.read_log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["slot"], "group_topic")

    def test_share_candidate_uses_public_life(self) -> None:
        stack = self.make({"proactive": {"daily_limit_group": 2, "min_interval_minutes": 0}})
        # 没有进行中的活动 → 先出话题候选（有话题时）
        self.speak(stack)
        decision = run(stack.proactive.patrol(now=NOON))
        self.assertEqual(decision["slot"], "group_topic")
        # 话题被本 tick 消耗（slots_today 防重）→ 下一 tick 轮到分享
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        snapshot = stack.life.snapshot(now=NOON)
        run(stack.life.replace_future(
            [
                {"start": "15:00", "end": "16:00", "title": "整理读书笔记", "location": "窗边"},
                {"start": "16:30", "end": "17:30", "title": "去河边散步", "location": "河边"},
            ],
            expected_revision=snapshot["revision"], now=NOON, trusted=True,
        ))
        decision2 = run(stack.proactive.patrol(now=NOON + timedelta(minutes=75)))
        self.assertIsNotNone(decision2)
        self.assertEqual(decision2["slot"], "group_share")
        self.assertIn("整理读书笔记", decision2["fragment"])
        self.assertIn("不猜测收件人", decision2["note"])
        self.assertIn("不 @ 全体", decision2["note"])

    def test_disabled_or_unregistered_group_never_proactive(self) -> None:
        stack = self.make()
        self.speak(stack)
        run(stack.social.save_group({"umo": GROUP, "enabled": False}, now=NOON))
        self.assertIsNone(run(stack.proactive.patrol(now=NOON)))
        # 未登记群：extra_groups 不含它，同样不发声
        stack2 = self.make()
        self.speak(stack2)
        run(stack2.social.delete_group(GROUP, now=NOON))
        self.assertIsNone(run(stack2.proactive.patrol(now=NOON)))

    def test_scope_private_still_blocks_groups(self) -> None:
        stack = self.make({"scope": {"mode": "private"}})
        self.speak(stack)
        self.assertIsNone(run(stack.proactive.patrol(now=NOON)))

    def test_daily_limit_and_slot_dedup_hold_for_groups(self) -> None:
        stack = self.make()
        self.speak(stack)
        first = run(stack.proactive.patrol(now=NOON))
        self.assertIsNotNone(first)
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        # 群每日上限默认 1：当天不该再有群候选
        second = run(stack.proactive.patrol(now=NOON + timedelta(minutes=30)))
        self.assertIsNone(second)

    def test_private_decision_does_not_come_from_group_channels(self) -> None:
        """scope=all 且主人私聊在名单里：私聊目标优先，群触发器只在私聊没得说时轮到。"""
        stack = self.make({"love_peers": ["10001"]})
        self.speak(stack)

        def add_contact(doc: dict) -> dict:
            doc["contacts"] = {"10001": {"umo": "aiocqhttp:FriendMessage:10001", "kind": "private"}}
            return doc

        run(stack.proactive.store.update(add_contact))
        run(stack.notebook.note_add(
            private_session(peer="10001"),
            kind="promise", text="周末一起去看展", about="10001",
            due_at=NOON.date().isoformat(), now=NOON,
        ))
        decision = run(stack.proactive.patrol(now=NOON))
        self.assertEqual(decision["slot"], "promise")  # 私聊内容优先于群聊

    def test_group_note_never_contains_private_love_diary(self) -> None:
        unique = "独一无二的私聊标记XYZZY"
        stack = self.make({"love_peers": ["10001"]})
        run(stack.diary.write_async(
            private_session(peer="10001", name="主人"), text=f"和他聊了心事 {unique}",
            now=NOON,
        ))
        self.speak(stack, text="这个群里的话题素材PURELY_GROUP_TOPIC")
        note = stack.proactive.group_note(
            slot="group_chat", umo=GROUP, fragment="小助手说：小澄，在吗",
            instruction="回一句", now=NOON,
        )
        self.assertIn("PURELY_GROUP_TOPIC", note)
        self.assertNotIn(unique, note)  # 恋爱日记的独特字符串绝不进群 note
        # 注入同理
        inject = compose.compose_prompt(
            stack.diary, group_session(group_id="555"),
            notebook=stack.notebook, state=stack.state, affinity=stack.affinity,
            life=stack.life, social=stack.social, now=NOON,
        )
        self.assertNotIn(unique, inject)


# ---- 注入与预算 ------------------------------------------------------------------


class ComposeSocialTests(TmpDirCase):
    def make(self) -> SimpleNamespace:
        stack = make_stack(self.root, {"scope": {"mode": "all"}, "social": {"self_names": "小澄"}})
        run(seed_enabled_group(stack))
        run(stack.social.observe_group(
            group_session(group_id="555", sender="10086", sender_name="小明"),
            text="今晚一起拼桌游吗", now=NOON,
        ))
        return stack

    def test_group_injection_contains_topics_within_budget(self) -> None:
        stack = self.make()
        text = compose.compose_prompt(
            stack.diary, group_session(group_id="555"),
            notebook=stack.notebook, state=stack.state, affinity=stack.affinity,
            life=stack.life, social=stack.social, now=NOON,
        )
        self.assertIn("群里最近", text)
        self.assertIn("拼桌游", text)
        self.assertIn("14:00", text)  # 来源时间
        self.assertLessEqual(len(text), compose.PROMPT_BUDGET + 1)

    def test_private_injection_never_gets_group_topics(self) -> None:
        stack = self.make()
        text = compose.compose_prompt(
            stack.diary, private_session(peer="10001"),
            notebook=stack.notebook, state=stack.state, affinity=stack.affinity,
            life=stack.life, social=stack.social, now=NOON,
        )
        self.assertNotIn("群里最近", text)
        self.assertNotIn("拼桌游", text)

    def test_other_group_topics_do_not_leak(self) -> None:
        stack = self.make()
        run(seed_enabled_group(stack, GROUP_B))
        text = compose.compose_prompt(
            stack.diary, group_session(group_id="666"),
            notebook=stack.notebook, state=stack.state, affinity=stack.affinity,
            life=stack.life, social=stack.social, now=NOON,
        )
        self.assertNotIn("拼桌游", text)  # A 群的话题不进 B 群

    def test_disabled_group_has_no_injection(self) -> None:
        stack = self.make()
        run(stack.social.save_group({"umo": GROUP, "enabled": False}, now=NOON))
        text = compose.compose_prompt(
            stack.diary, group_session(group_id="555"),
            notebook=stack.notebook, state=stack.state, affinity=stack.affinity,
            life=stack.life, social=stack.social, now=NOON,
        )
        self.assertNotIn("群里最近", text)


# ---- 面板数据 --------------------------------------------------------------------


class PanelTests(TmpDirCase):
    def test_payload_shape(self) -> None:
        stack = make_stack(self.root, {"scope": {"mode": "all"}, "social": {"self_names": "小澄"}})
        run(seed_enabled_group(stack))
        run(seed_bot(stack))
        run(stack.social.observe_group(
            group_session(group_id="555", sender="10086", sender_name="小明"),
            text="大家好", now=NOON,
        ))
        run(stack.social.chat_decision(
            group_session(group_id="555", sender="bot1", sender_name="小助手"),
            text="小澄，在吗", now=NOON,
        ))
        payload = stack.social.panel_payload(now=NOON)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scope_mode"], "all")
        group = payload["groups"][0]
        self.assertEqual(group["umo"], GROUP)
        self.assertTrue(group["enabled"])
        self.assertEqual(len(group["participants"]), 2)  # bot1 登记 + 小明自动补录
        self.assertEqual(group["topics"][0]["text"], "大家好")
        self.assertTrue(payload["decisions"])  # 短聊判定有记录
        self.assertIn("config", payload)
        self.assertEqual(payload["self_names"], ["小澄"])


# ---- 设置与 scope 接线 -------------------------------------------------------------


class SettingsScopeTests(unittest.TestCase):
    def test_social_defaults_and_clamps(self) -> None:
        resolved = settings_mod.load_settings({"social": {"max_rounds": 99, "topic_max_items": 0}})
        self.assertEqual(resolved.social["max_rounds"], 10)  # 夹紧到上限
        self.assertEqual(resolved.social["topic_max_items"], 1)
        self.assertTrue(any("social.max_rounds" in w for w in resolved.warnings))

    def test_parse_self_names(self) -> None:
        self.assertEqual(chat.parse_self_names("小澄、澄澄，澄仔 x"), ("小澄", "澄澄", "澄仔", "x"))
        self.assertEqual(chat.parse_self_names(""), ())
        self.assertEqual(chat.parse_self_names("  同名  同名  "), ("同名",))

    def test_extra_groups_default_is_noop(self) -> None:
        targets = scope.proactive_targets(
            "all", contacts={}, love_peers=(), known_groups=(), extra_groups=(),
        )
        self.assertEqual(targets, [])

    def test_extra_groups_extend_only_in_all_mode(self) -> None:
        targets = scope.proactive_targets(
            "all", contacts={}, love_peers=(), known_groups=(), extra_groups=(GROUP,),
        )
        self.assertEqual(targets, [(GROUP, "group")])
        for mode in ("owner", "private"):
            targets = scope.proactive_targets(
                mode, contacts={}, love_peers=(), known_groups=(), extra_groups=(GROUP,),
            )
            self.assertEqual(targets, [])


class SocialProactiveGateTests(TmpDirCase):
    def make(self, config: dict | None = None, *, bot_id: str = "bot-9988") -> SimpleNamespace:
        raw = {
            "scope": {"mode": "all"},
            "social": {"self_names": "小澄"},
            "proactive": {"daily_limit_group": 3, "min_interval_minutes": 0},
            **(config or {}),
        }
        stack = make_stack(self.root, raw)
        run(seed_enabled_group(stack))
        run(seed_bot(stack, pid=bot_id, name="登记助手"))
        return stack

    def bot_message(self, bot_id: str = "bot-9988"):
        return group_session(group_id="555", sender=bot_id, sender_name="登记助手")

    def test_manages_message_is_read_only_and_checks_full_social_eligibility(self) -> None:
        stack = self.make()
        before = stack.social.store.read()
        self.assertTrue(stack.social.manages_message(self.bot_message(), "小澄，在吗"))
        self.assertFalse(stack.social.manages_message(self.bot_message(), "有人吗"))
        self.assertFalse(stack.social.manages_message(private_session(), "小澄，在吗"))
        self.assertEqual(stack.social.store.read(), before)
        run(stack.social.save_participant(
            GROUP, {"id": "bot-9988", "is_bot": True, "allow_interaction": False}, now=NOON
        ))
        self.assertFalse(stack.social.manages_message(self.bot_message(), "小澄，在吗"))

    def test_zero_group_quota_rejects_before_consuming_chat_round(self) -> None:
        stack = self.make({"proactive": {"daily_limit_group": 0}})
        session = self.bot_message()
        text = "小澄，在吗，这段原文不应进决策日志 SECRET-CONTENT"
        self.assertTrue(stack.social.manages_message(session, text))
        decision = run(stack.proactive.group_chat_decision(session, text, NOON))
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("配额已满", decision["reason"])
        self.assertNotIn("chats", stack.social.store.read())
        self.assertNotIn(GROUP, stack.proactive._pending_log)
        logged = stack.social.panel_payload(now=NOON)["decisions"][-1]
        self.assertEqual(logged["action"], "ignore")
        self.assertIn("配额已满", logged["reason"])
        self.assertEqual(logged["fragment"], "")
        self.assertNotIn("SECRET-CONTENT", str(logged))

    def test_disabled_proactive_and_quiet_hours_do_not_consume_chat_round(self) -> None:
        disabled = self.make({"subsystems": {"proactive": False}})
        session = self.bot_message()
        decision = run(disabled.proactive.group_chat_decision(session, "小澄，在吗", NOON))
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("未启用", decision["reason"])
        self.assertNotIn("chats", disabled.social.store.read())
        self.assertIn("未启用", disabled.social.panel_payload(now=NOON)["decisions"][-1]["reason"])

        quiet = self.make()
        quiet.state.is_late_night = lambda **_kwargs: True
        decision = run(quiet.proactive.group_chat_decision(session, "小澄，在吗", NOON))
        self.assertEqual(decision["action"], "ignore")
        self.assertIn("免打扰", decision["reason"])
        self.assertNotIn("chats", quiet.social.store.read())
        self.assertIn("免打扰", quiet.social.panel_payload(now=NOON)["decisions"][-1]["reason"])

    def test_minimum_interval_rejects_before_next_chat_round(self) -> None:
        stack = self.make({"proactive": {"daily_limit_group": 3, "min_interval_minutes": 30}})
        session = self.bot_message()
        first = run(stack.proactive.group_chat_decision(session, "小澄，第一句", NOON))
        self.assertEqual(first["action"], "wake")
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        before = stack.social.store.read()["chats"][GROUP]
        second = run(stack.proactive.group_chat_decision(
            session, "小澄，十分钟后的第二句", NOON + timedelta(minutes=10)
        ))
        self.assertEqual(second["action"], "ignore")
        self.assertIn("不足 30 分钟", second["reason"])
        after = stack.social.store.read()["chats"][GROUP]
        self.assertEqual(after, before)

    def test_commit_rechecks_minimum_interval_under_store_lock(self) -> None:
        stack = self.make({"proactive": {"daily_limit_group": 3, "min_interval_minutes": 30}})

        def mark_recently_sent(doc: dict) -> dict:
            doc["sessions"] = {GROUP: {"last_sent_at": NOON.isoformat(timespec="seconds")}}
            return doc

        run(stack.proactive.store.update(mark_recently_sent))
        decision = {
            "kind": "wake", "slot": "group_chat", "umo": GROUP,
            "fragment": "必须由锁内间隔闸拦下",
        }
        committed = run(stack.proactive._commit(
            decision, dict(stack.settings.proactive), NOON + timedelta(minutes=10)
        ))
        self.assertFalse(committed)
        entry = gate.entry_of(stack.proactive.store.read(), GROUP)
        self.assertNotIn("today_count", entry)
        self.assertNotIn("last_slot", entry)

    def test_round_limit_is_enforced_through_final_proactive_decision(self) -> None:
        stack = self.make({"social": {"self_names": "小澄", "max_rounds": 1}})
        session = self.bot_message()
        first = run(stack.proactive.group_chat_decision(session, "小澄，第一句", NOON))
        self.assertEqual(first["action"], "wake")
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        second = run(stack.proactive.group_chat_decision(
            session, "小澄，第二句", NOON + timedelta(minutes=1)
        ))
        self.assertEqual(second["action"], "ignore")
        self.assertIn("1 轮上限", second["reason"])
        self.assertIsNone(stack.social.store.read()["chats"][GROUP]["active"])

    def test_wake_note_keeps_real_bot_id_voice_decision_and_two_phase_confirmation(self) -> None:
        bot_id = "bot-真实-9988"
        stack = self.make({"voice": {"mode": "voice", "probability": 100}}, bot_id=bot_id)
        session = self.bot_message(bot_id)
        decision = run(stack.proactive.group_chat_decision(session, "小澄，在吗", NOON))
        self.assertEqual(decision["action"], "wake")
        self.assertEqual(decision["bot_id"], bot_id)
        self.assertIn(bot_id, decision["note"])
        self.assertTrue(decision["speak"])
        self.assertTrue(stack.proactive.pending_voice(GROUP))
        self.assertEqual(stack.proactive._pending_log[GROUP]["slot"], "group_chat")
        entry = gate.entry_of(stack.proactive.store.read(), GROUP)
        self.assertEqual(entry.get("today_count"), 1)
        self.assertNotIn("last_sent_at", entry)
        self.assertEqual(stack.proactive.store.read_log_lines(), [])

        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        entry = gate.entry_of(stack.proactive.store.read(), GROUP)
        self.assertEqual(entry["last_sent_at"], NOON.isoformat(timespec="seconds"))
        self.assertEqual(stack.proactive.store.read_log_lines()[0]["slot"], "group_chat")

    def test_pending_expires_lazily_and_unknown_old_shape_does_not_block(self) -> None:
        config = {"social": {"self_names": "小澄", "session_window_minutes": 60}}
        stack = self.make(config)
        session = self.bot_message()
        first = run(stack.proactive.group_chat_decision(session, "小澄，第一句", NOON))
        self.assertEqual(first["action"], "wake")
        self.assertIn("reserved_at", stack.proactive._pending_log[GROUP])

        blocked = run(stack.proactive.group_chat_decision(
            session, "小澄，第二句", NOON + timedelta(minutes=9)
        ))
        self.assertEqual(blocked["action"], "ignore")
        self.assertIn("待确认", blocked["reason"])

        expired = run(stack.proactive.group_chat_decision(
            session, "小澄，超过窗口后的新消息", NOON + timedelta(minutes=11)
        ))
        self.assertEqual(expired["action"], "wake")

        legacy = self.make(config)
        legacy.proactive._pending_log[GROUP] = {"slot": "group_chat", "fragment": "legacy"}
        legacy.proactive._pending_speak[GROUP] = True
        resumed = run(legacy.proactive.group_chat_decision(
            self.bot_message(), "小澄，旧格式不能永远锁住", NOON
        ))
        self.assertEqual(resumed["action"], "wake")
        self.assertIn("reserved_at", legacy.proactive._pending_log[GROUP])

    def test_reject_pending_clears_speak_without_refunding_quota_or_confirming_send(self) -> None:
        stack = self.make({"voice": {"mode": "voice", "probability": 100}})
        first = run(stack.proactive.group_chat_decision(
            self.bot_message(), "小澄，第一句", NOON
        ))
        self.assertEqual(first["action"], "wake")
        self.assertTrue(stack.proactive.pending_voice(GROUP))
        self.assertTrue(run(stack.proactive.reject_pending(GROUP)))
        self.assertNotIn(GROUP, stack.proactive._pending_log)
        self.assertFalse(stack.proactive.pending_voice(GROUP))
        entry = gate.entry_of(stack.proactive.store.read(), GROUP)
        self.assertEqual(entry["today_count"], 1)
        self.assertNotIn("last_sent_at", entry)
        self.assertEqual(stack.proactive.store.read_log_lines(), [])

        second = run(stack.proactive.group_chat_decision(
            self.bot_message(), "小澄，明确失败后可重试", NOON + timedelta(minutes=1)
        ))
        self.assertEqual(second["action"], "wake")
        self.assertEqual(gate.entry_of(stack.proactive.store.read(), GROUP)["today_count"], 2)

    def test_duplicate_rejection_is_visible_without_storing_message_text(self) -> None:
        stack = self.make()
        session = self.bot_message()
        text = "小澄，重复内容 PRIVATE-MARKER"
        first = run(stack.proactive.group_chat_decision(session, text, NOON))
        self.assertEqual(first["action"], "wake")
        run(stack.proactive.confirm_sent(GROUP, now=NOON))
        second = run(stack.proactive.group_chat_decision(session, text, NOON + timedelta(minutes=1)))
        self.assertEqual(second["action"], "ignore")
        self.assertIn("重复消息", second["reason"])
        logged = stack.social.panel_payload(now=NOON + timedelta(minutes=1))["decisions"][-1]
        self.assertEqual(logged["action"], "ignore")
        self.assertEqual(logged["fragment"], "")
        self.assertNotIn("PRIVATE-MARKER", str(logged))

    def test_concurrent_chat_and_patrol_do_not_overwrite_pending_action(self) -> None:
        stack = self.make()
        run(stack.social.observe_group(
            group_session(group_id="555", sender="10086", sender_name="小明"),
            text="今晚一起去看电影吗", now=NOON,
        ))
        session = self.bot_message()

        async def race():
            return await asyncio.gather(
                stack.proactive.group_chat_decision(session, "小澄，想一起聊吗", NOON),
                stack.proactive.patrol(now=NOON),
            )

        chat_result, patrol_result = run(race())
        self.assertEqual(sum([
            chat_result.get("action") == "wake",
            patrol_result is not None,
        ]), 1)
        pending = stack.proactive._pending_log[GROUP]
        self.assertIn(pending["slot"], {"group_chat", "group_topic"})
        self.assertEqual(gate.today_count(
            gate.entry_of(stack.proactive.store.read(), GROUP), NOON.date().isoformat()
        ), 1)

    def test_topic_switch_stops_topic_collection_but_keeps_participant_enrollment(self) -> None:
        stack = self.make({"social": {"self_names": "小澄", "topic_enabled": False}})
        outcome = run(stack.social.observe_group(
            group_session(group_id="555", sender="10086", sender_name="小明"),
            text="这段话不能成为话题素材", now=NOON,
        ))
        self.assertTrue(outcome["observed"])
        doc = stack.social.store.read()
        self.assertNotIn(GROUP, doc.get("topics", {}))
        self.assertIn("10086", doc["participants"][GROUP])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
