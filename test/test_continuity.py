"""续聊连续性回归（第 21 步）：

- 检索 query 与事件信号拆开：普通原文只当 query，不再触发 `event_hint`；
- 被动轮注入带上当前私聊归属人的日记正文素材（含当天、带日期、标明历史）；
- 素材只走 `_own_entries` + `visible`，群聊 / 另一人 / 恋爱权限不外泄；
- 总预算仍是 400，续聊素材优先于机械催写。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import compose  # noqa: E402
from core.diary.api import RECALL_BUDGET  # noqa: E402
from support import NOW, TmpDirCase, group_session, make_diary, private_session  # noqa: E402
from test_main import PluginCase, _FakeEvent, _parts_text  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class _SpyDiary:
    """只记录 compose 怎么调它；不实现 prompt_parts，走旧实现降级路径。"""

    def __init__(self) -> None:
        self.event_texts: list[str] = []
        self.recall_queries: list[str] = []
        self.prompt_calls = 0

    def prompt_line(self, session, *, now=None) -> str:
        self.prompt_calls += 1
        return "【日记】状态行"

    def recall_line(self, session, *, query="", now=None, budget=180) -> str:
        self.recall_queries.append(query)
        return ""

    def event_hint(self, session, *, strong_text="", exchange_who="", now=None) -> str:
        self.event_texts.append(strong_text)
        return ""


class _SpyNotebook:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def prompt_lines(
        self, session, *, now=None, query="", promise_budget=140, fact_budget=100
    ) -> tuple[str, str]:
        self.queries.append(query)
        return "", ""


class QueryEventSplitTest(TmpDirCase):
    def test_recall_budget_constants_agree(self) -> None:
        self.assertEqual(compose.RECALL_BUDGET, RECALL_BUDGET)

    def test_ordinary_message_is_query_not_event(self) -> None:
        diary, notebook = _SpyDiary(), _SpyNotebook()
        compose.compose_prompt(
            diary,
            private_session(),
            notebook=notebook,
            now=NOW,
            query_text="下午好",
            strong_text="",
        )
        self.assertEqual(diary.event_texts, [""], "普通问候不能当强事件")
        self.assertEqual(diary.recall_queries, ["下午好"], "原文仍要用于日记素材检索")
        self.assertEqual(notebook.queries, ["下午好"], "原文仍要用于小本本检索")
        self.assertEqual(diary.prompt_calls, 1)

    def test_explicit_event_still_reaches_event_hint(self) -> None:
        diary, notebook = _SpyDiary(), _SpyNotebook()
        compose.compose_prompt(
            diary,
            private_session(),
            notebook=notebook,
            now=NOW,
            query_text="你还记得吗",
            strong_text="明天面试",
        )
        self.assertEqual(diary.event_texts, ["明天面试"], "真实明确事件调用保持兼容")
        self.assertEqual(diary.recall_queries, ["你还记得吗"], "检索用 query，不用事件信号")
        self.assertEqual(notebook.queries, ["你还记得吗"])

    def test_legacy_strong_text_still_works_as_query(self) -> None:
        """旧调用方只传 strong_text：检索行为不变（主动轮 / 既有测试双兼容）。"""
        diary, notebook = _SpyDiary(), _SpyNotebook()
        compose.compose_prompt(
            diary, private_session(), notebook=notebook, now=NOW, strong_text="草莓蛋糕"
        )
        self.assertEqual(diary.recall_queries, ["草莓蛋糕"])
        self.assertEqual(notebook.queries, ["草莓蛋糕"])


class RecallMaterialTest(TmpDirCase):
    def _seed_interview(self, diary) -> None:
        run(
            diary.write_async(
                private_session(),
                text="明天要去面试，有点紧张",
                now=NOW - timedelta(days=1),
            )
        )
        run(
            diary.write_async(
                private_session(),
                text="今天面试完了，聊得还行",
                now=NOW - timedelta(hours=2),
            )
        )

    def test_yesterday_and_today_are_both_recalled_with_dates(self) -> None:
        diary = make_diary(self.root)
        self._seed_interview(diary)
        line = diary.recall_line(
            private_session(), query="你还记得我今天什么事吗", now=NOW
        )
        self.assertIn("历史素材", line)
        self.assertIn("不是现在", line, "必须明确这是历史素材")
        self.assertIn("2026-10-03 21:30", line, "昨晚的面试要带日期")
        self.assertIn("2026-10-04 19:30", line, "当天最新一段不能被排除")
        self.assertIn("明天要去面试", line, "逐字引用，不总结")
        self.assertIn("今天面试完了", line)
        self.assertLessEqual(len(line), RECALL_BUDGET)

    def test_today_latest_is_kept_even_with_older_entries(self) -> None:
        diary = make_diary(self.root)
        for offset in (40, 30, 20):
            run(
                diary.write_async(
                    private_session(),
                    text=f"{offset} 天前的旧段落",
                    now=NOW - timedelta(days=offset),
                )
            )
        run(
            diary.write_async(
                private_session(), text="今天刚刚发生的事", now=NOW - timedelta(hours=1)
            )
        )
        line = diary.recall_line(private_session(), query="今天", now=NOW)
        self.assertIn("今天刚刚发生的事", line)
        self.assertIn("2026-10-04 20:30", line)

    def test_query_can_pull_relevant_older_entry(self) -> None:
        diary = make_diary(self.root)
        run(
            diary.write_async(
                private_session(),
                text="我养了一只猫叫团子",
                now=NOW - timedelta(days=30),
            )
        )
        run(
            diary.write_async(
                private_session(), text="今天在加班", now=NOW - timedelta(hours=1)
            )
        )
        line = diary.recall_line(private_session(), query="团子最近怎么样", now=NOW)
        self.assertIn("团子", line, "与当前 query 相关的旧正文也要能取到")
        self.assertIn("今天在加班", line)

    def test_budget_is_hard_bounded(self) -> None:
        diary = make_diary(self.root)
        self._seed_interview(diary)
        line = diary.recall_line(private_session(), query="面试", now=NOW, budget=80)
        self.assertLessEqual(len(line), 80)
        self.assertIn("历史素材", line)
        self.assertTrue(line.splitlines()[-1].startswith("2026-"), line)

    def test_caller_cannot_enlarge_the_recall_cap(self) -> None:
        diary = make_diary(self.root)
        self._seed_interview(diary)
        line = diary.recall_line(private_session(), query="面试", now=NOW, budget=10_000)
        self.assertLessEqual(len(line), RECALL_BUDGET)

    def test_no_entries_renders_empty(self) -> None:
        diary = make_diary(self.root)
        self.assertEqual(diary.recall_line(private_session(), query="面试", now=NOW), "")

    def test_group_and_other_people_never_get_private_material(self) -> None:
        diary = make_diary(self.root)
        self._seed_interview(diary)
        run(
            diary.write_async(
                private_session("20002", "另一个人"),
                text="另一个人今天在加班",
                now=NOW - timedelta(hours=1),
            )
        )

        self.assertEqual(
            diary.recall_line(group_session(), query="面试", now=NOW), "", "群聊不念私聊素材"
        )
        other = diary.recall_line(
            private_session("20002", "另一个人"), query="面试", now=NOW
        )
        self.assertIn("加班", other)
        self.assertNotIn("面试", other, "另一人的私聊不能拿到这个人的日记")

        group_prompt = diary.prompt_line(group_session(), now=NOW)
        self.assertNotIn("上一条", group_prompt, "群聊连 40 字开头都不念")
        self.assertNotIn("面试", group_prompt)
        private_prompt = diary.prompt_line(private_session(), now=NOW)
        self.assertIn("上一条是 2026-10-03", private_prompt, "私聊仍保留上一条线索")

    def test_love_book_stays_with_its_reader(self) -> None:
        writer = make_diary(self.root, {"love_peers": ["10001"]})
        run(
            writer.write_async(
                private_session("10001"),
                text="只给最爱的人说的话",
                now=NOW - timedelta(hours=2),
            )
        )
        reader = make_diary(self.root, {"love_peers": ["99999"]})
        self.assertEqual(
            reader.recall_line(private_session("10001"), query="最爱", now=NOW),
            "",
            "恋爱日记权限收紧后，同一个人也不能再从普通入口读到",
        )


class ComposeRecallPriorityTest(TmpDirCase):
    def test_recall_beats_mechanical_nudge_without_extra_budget(self) -> None:
        diary = make_diary(self.root)
        run(
            diary.write_async(
                private_session(),
                text="明天要去面试，有点紧张",
                now=NOW - timedelta(days=1),
            )
        )
        run(
            diary.write_async(
                private_session(),
                text="今天面试完了，聊得还行",
                now=NOW - timedelta(hours=2),
            )
        )

        class Notebook:
            def prompt_lines(
                self, session, *, now=None, query="", promise_budget=140, fact_budget=100
            ):
                return "P" * 110, "F" * 50

        class State:
            def rhythm_line(self, *, now=None):
                return "S" * 40

            def mood_line(self, session, *, now=None):
                return "M" * 15

        class Affinity:
            def line(self, session, *, now=None):
                return "（泛泛之交）"

            def trace_line(self, session, *, now=None):
                return "T" * 60

        text = compose.compose_prompt(
            diary,
            private_session(),
            notebook=Notebook(),
            state=State(),
            affinity=Affinity(),
            now=NOW,
            query_text="面试",
            budget=compose.PROMPT_BUDGET,
        )
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)
        self.assertIn("历史素材", text, "续聊素材优先保留")
        self.assertIn("面试", text)
        self.assertNotIn("【日记】", text, "机械状态/催写档在预算不足时先让位")
        self.assertNotIn("diary_write", text)


class _PassiveTurnCase(PluginCase):
    def _inject(self, plugin, event) -> str:
        req = sys.modules["astrbot.api.provider"].ProviderRequest()
        run(plugin.inject_diary_hint(event, req))
        return _parts_text(req)

    def _seed_interview(self, plugin) -> None:
        run(
            plugin.diary.write_async(
                private_session(),
                text="明天要去面试，有点紧张",
                now=NOW - timedelta(days=1),
            )
        )
        run(
            plugin.diary.write_async(
                private_session(),
                text="今天面试完了，聊得还行",
                now=NOW - timedelta(hours=2),
            )
        )


class PassiveContinuityTest(_PassiveTurnCase):
    def test_ordinary_greeting_does_not_trigger_event_nudge_but_query_still_reaches_notes(
        self,
    ) -> None:
        plugin = self.make_plugin()
        plugin._now = lambda: NOW
        run(plugin.note_add(_FakeEvent(), kind="fact", text="喜欢吃草莓蛋糕"))

        greeting = self._inject(plugin, _FakeEvent(message_str="下午好"))
        self.assertNotIn("刚才这事要是想留着", greeting, "普通问候不触发事件催写")
        self.assertNotIn("刚跟", greeting)

        relevant = self._inject(plugin, _FakeEvent(message_str="我今天想吃草莓蛋糕"))
        self.assertIn("喜欢吃草莓蛋糕", relevant, "原文仍作为小本本检索 query")
        self.assertNotIn("刚才这事要是想留着", relevant)

    def test_today_question_recalls_yesterday_and_today_with_history_framing(self) -> None:
        plugin = self.make_plugin()
        plugin._now = lambda: NOW
        self._seed_interview(plugin)

        text = self._inject(plugin, _FakeEvent(message_str="你还记得我今天什么事吗"))
        self.assertIn("历史素材", text)
        self.assertIn("不是现在", text)
        self.assertIn("2026-10-03 21:30", text)
        self.assertIn("2026-10-04 19:30", text)
        self.assertIn("明天要去面试", text)
        self.assertIn("今天面试完了", text)
        self.assertNotIn("刚才这事要是想留着", text)
        self.assertLessEqual(len(text), compose.PROMPT_BUDGET)

    def test_other_private_and_group_do_not_get_the_material(self) -> None:
        plugin = self.make_plugin({"scope": {"mode": "all"}})
        plugin._now = lambda: NOW
        self._seed_interview(plugin)

        other = self._inject(
            plugin,
            _FakeEvent(
                "aiocqhttp:FriendMessage:20002",
                type="FriendMessage",
                session_id="20002",
                sender_id="20002",
                sender_name="另一个人",
                message_str="你还记得我今天什么事吗",
            ),
        )
        self.assertNotIn("面试", other)
        self.assertNotIn("历史素材", other)

        group = self._inject(
            plugin,
            _FakeEvent(
                "aiocqhttp:GroupMessage:555",
                type="GroupMessage",
                session_id="555",
                group_id="555",
                sender_id="10001",
                message_str="你还记得我今天什么事吗",
            ),
        )
        self.assertNotIn("面试", group)
        self.assertNotIn("历史素材", group)


if __name__ == "__main__":
    unittest.main(verbosity=2)
