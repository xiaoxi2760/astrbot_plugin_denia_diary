"""日记系统行为测试（第 1 步验收）。

四层：格式契约 / 文件层 / 语义入口 / **回归清单**（原包实测踩过的坑）。

    $env:PYTHONDONTWRITEBYTECODE=1; $env:PYTHONUTF8=1
    py -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import storage  # noqa: E402
from core.diary import format as fmt  # noqa: E402
from core.session import Session  # noqa: E402
from support import (  # noqa: E402
    NOW,
    TZ,
    TmpDirCase,
    group_session,
    make_diary,
    private_session,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 格式契约


class FormatContractTest(unittest.TestCase):
    def test_head_line_roundtrip(self) -> None:
        head = fmt.make_head_line("2026-10-04", "21:30", "开心", "和某某")
        self.assertEqual(head, "2026-10-04 21:30（开心）〔和某某〕")
        match = fmt.HEAD_RE.match(head)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), "2026-10-04")
        self.assertEqual(match.group(3), "开心")
        self.assertEqual(match.group(4), "和某某")

    def test_head_line_without_mood_or_tag(self) -> None:
        self.assertEqual(fmt.make_head_line("2026-10-04", "09:05"), "2026-10-04 09:05")

    def test_long_tag_is_truncated_and_still_parses(self) -> None:
        """回归 P1-b：写入侧必须与正则同源，否则整段读不出来。"""
        long_tag = "群·" + "很长的群名" * 20  # 远超 40 字
        head = fmt.make_head_line("2026-10-04", "21:30", "", long_tag)
        self.assertLessEqual(len(fmt.sanitize_tag(long_tag)), fmt.TAG_MAX)
        self.assertIsNotNone(fmt.HEAD_RE.match(head), head)

    def test_tag_strips_bracket_and_newlines(self) -> None:
        self.assertEqual(fmt.sanitize_tag("和某〕某\n某"), "和某 某 某")
        self.assertNotIn("〕", fmt.sanitize_tag("a〕b"))

    def test_mood_is_sanitized_and_capped(self) -> None:
        self.assertEqual(fmt.sanitize_mood("（开心）"), "开心")
        self.assertEqual(fmt.sanitize_mood("a\nb"), "a b")
        self.assertLessEqual(len(fmt.sanitize_mood("很" * 50)), fmt.MOOD_MAX)

    def test_scope_of_new_scheme_is_exact(self) -> None:
        self.assertEqual(fmt.scope_of("群·和平精英交流群"), "group")
        self.assertEqual(fmt.scope_of("和某某"), "private")
        self.assertEqual(fmt.scope_of(""), "")

    def test_scope_of_legacy_scheme_keeps_old_heuristic(self) -> None:
        """旧条目（无 ``群·`` 前缀）仍走启发式 —— 明知会把"和平…"误判成私聊，但要能读。"""
        self.assertEqual(fmt.scope_of("和平精英交流群"), "private")
        self.assertEqual(fmt.scope_of("某某群"), "group")

    def test_split_segments(self) -> None:
        text = (
            "2026-10-01 10:00（平静）〔群·甲〕\n第一段正文\n\n"
            "2026-10-02 11:00〔和乙〕\n第二段\n还是第二段\n\n"
        )
        segments = fmt.split_segments(text)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].text, "第一段正文")
        self.assertEqual(segments[1].text, "第二段\n还是第二段")
        self.assertEqual(segments[1].head_idx, 3)
        self.assertEqual(segments[1].scope, "private")

    def test_parse_entries_skips_empty_segments(self) -> None:
        text = "2026-10-01 10:00（平静）\n\n2026-10-02 11:00（开心）\n有正文\n"
        entries = fmt.parse_entries(text, fmt.LOVE)
        self.assertEqual(len(entries), 2)  # 第一条有心境，留着
        self.assertTrue(all(e.book == fmt.LOVE for e in entries))

    def test_keep_trailing_newlines(self) -> None:
        self.assertEqual(fmt.keep_trailing_newlines("a\nb", "x\n\n\n"), "a\nb\n\n\n")
        self.assertEqual(fmt.keep_trailing_newlines("a\n\n\n", "x"), "a\n\n")

    def test_normalize_body_literal_backslash_n_and_blank_lines(self) -> None:
        self.assertEqual(
            fmt.normalize_body("第一段\\n\\n第二段", 1200), "第一段\n\n第二段"
        )
        self.assertEqual(fmt.normalize_body("a\r\nb", 1200), "a\n\nb")

    def test_normalize_body_clamps_to_max_chars(self) -> None:
        self.assertEqual(fmt.normalize_body("字" * 50, 10), "字" * 10)

    def test_render_marks_love_only_in_text(self) -> None:
        entries = [
            fmt.Entry("2026-10-01", "10:00", "开心", "和某某", "正文", fmt.LOVE),
            fmt.Entry("2026-10-01", "11:00", "", "群·甲", "另一条", fmt.NORMAL),
        ]
        plain = fmt.render(entries)
        self.assertNotIn("❤️", plain)
        marked = fmt.render(entries, mark_love=True, footer="\n\n（脚注）")
        self.assertTrue(marked.startswith("❤️ 【2026-10-01 10:00 · 开心】〔和某某〕"))
        self.assertTrue(marked.endswith("（脚注）"))

    def test_within_window_rejects_old_and_future(self) -> None:
        old = fmt.Segment("h", 0, 0, "2026-01-01", "10:00", "", "", "x")
        recent = fmt.Segment("h", 0, 0, "2026-10-04", "10:00", "", "", "x")
        future = fmt.Segment("h", 0, 0, "2027-01-01", "10:00", "", "", "x")
        self.assertFalse(fmt.within_window(old, NOW, 7, TZ))
        self.assertTrue(fmt.within_window(recent, NOW, 7, TZ))
        self.assertFalse(fmt.within_window(future, NOW, 7, TZ))

    def test_find_segment_lists_recent_when_missing(self) -> None:
        segments = fmt.split_segments("2026-10-04 10:00\n甲\n\n2026-10-04 11:00\n乙\n\n")
        seg, error = fmt.find_segment(segments, "不存在", NOW, 7, TZ)
        self.assertIsNone(seg)
        self.assertIn("没找到", error)
        self.assertIn("11:00", error)

    def test_find_segment_empty_book(self) -> None:
        seg, error = fmt.find_segment([], "x", NOW, 7, TZ)
        self.assertIsNone(seg)
        self.assertEqual(error, "这本里还没有段落")

    def test_rewrite_preserves_head_and_trailing_newlines(self) -> None:
        text = "2026-10-04 10:00（平静）〔群·甲〕\n旧正文\n\n"
        result = fmt.rewrite_segment(
            text, match="旧正文", new_body="新正文", now=NOW, within_days=7, tz=TZ
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.text.startswith("2026-10-04 10:00（平静）〔群·甲〕\n新正文"))
        self.assertTrue(result.text.endswith("\n\n"))
        self.assertEqual(result.before, "旧正文")

    def test_rewrite_rejects_empty_body(self) -> None:
        text = "2026-10-04 10:00\n旧正文\n\n"
        result = fmt.rewrite_segment(
            text, match="旧", new_body="  ", now=NOW, within_days=7, tz=TZ
        )
        self.assertFalse(result.ok)
        self.assertIn("delete", result.error)

    def test_delete_collapses_blank_lines(self) -> None:
        text = (
            "2026-10-01 10:00\n甲\n\n2026-10-02 11:00\n要删的\n\n2026-10-03 12:00\n丙\n\n"
        )
        result = fmt.delete_segment(
            text, match="要删的", now=NOW, within_days=7, tz=TZ
        )
        self.assertTrue(result.ok)
        self.assertNotIn("要删的", result.text)
        self.assertNotIn("\n\n\n", result.text)
        self.assertEqual(len(fmt.split_segments(result.text)), 2)


# ---------------------------------------------------------------- 文件层


class StoreTest(TmpDirCase):
    def test_append_writes_lf_and_trailing_newline(self) -> None:
        diary = make_diary(self.root)
        run(
            diary.store.append(
                fmt.NORMAL, date="2026-10-04", time="21:30", mood="开心", tag="和某某", body="第一条"
            )
        )
        raw = diary.layout.diary.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith("\n\n".encode()))
        self.assertEqual(len(diary.entries()), 1)

    def test_append_after_file_without_trailing_newline(self) -> None:
        diary = make_diary(self.root)
        storage.atomic_write_text(
            diary.layout.diary, "2026-10-01 10:00〔群·甲〕\n之前写的"
        )
        run(
            diary.store.append(
                fmt.NORMAL, date="2026-10-04", time="21:30", body="新的一条"
            )
        )
        self.assertIn("之前写的\n2026-10-04", diary.store.read(fmt.NORMAL))
        self.assertEqual(len(diary.entries()), 2)

    def test_edit_backs_up_original_into_trash(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(group_session(), text="第一版", now=NOW))
        result = run(
            diary.edit_for(group_session(), action="rewrite", match="第一版", text="第二版", now=NOW)
        )
        self.assertTrue(result["ok"])
        backups = list(diary.layout.trash_dir.glob("*-rewrite-normal.txt"))
        self.assertEqual(len(backups), 1)
        self.assertIn("第一版", backups[0].read_text(encoding="utf-8"))

    def test_cache_is_invalidated_after_write(self) -> None:
        diary = make_diary(self.root)
        self.assertEqual(diary.store.read(fmt.NORMAL), "")
        run(diary.write_async(group_session(), text="写进去了", now=NOW))
        self.assertIn("写进去了", diary.store.read(fmt.NORMAL))


# ---------------------------------------------------------------- 语义入口


class DiaryApiTest(TmpDirCase):
    def test_write_then_read_uses_session_labels(self) -> None:
        diary = make_diary(self.root)
        result = run(diary.write_async(group_session(name="某某群"), text="今天群里挺开心", mood="开心", now=NOW))
        self.assertTrue(result["ok"])
        self.assertEqual(result["book"], fmt.NORMAL)
        self.assertEqual(result["date"], "2026-10-04")
        self.assertEqual(result["time"], "21:30")

        text = diary.read_for(group_session(name="某某群"), now=NOW)["text"]
        self.assertIn("〔群·某某群〕", text)
        self.assertIn("今天群里挺开心", text)

        run(diary.write_async(private_session(name="某某"), text="只对他说", now=NOW))
        text = diary.read_for(private_session(name="某某"), now=NOW)["text"]
        self.assertIn("〔和某某〕", text)

    def test_love_peers_routes_to_second_book(self) -> None:
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        peer = run(diary.write_async(private_session("10001"), text="只对他说的话", now=NOW))
        self.assertEqual(peer["book"], fmt.LOVE)
        other = run(diary.write_async(private_session("20002", "另一个人"), text="普通的一条", now=NOW))
        self.assertEqual(other["book"], fmt.NORMAL)
        self.assertTrue(diary.layout.love_diary.is_file())

    def test_wake_without_sender_id_still_routes_to_love_book(self) -> None:
        """回归（2026-10-05 真机 10:00 段）：定时唤醒没有 sender_id，私聊 session_id 就是对端。

        不兜底的话这段会掉进普通日记那本，头行还会顶着合成名字 ``Scheduler``。
        """
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        wake = Session(
            umo="aiocqhttp:FriendMessage:10001",
            message_type="FriendMessage",
            session_id="10001",
            sender_name="Scheduler",
        )
        result = run(diary.write_async(wake, text="上午十点写的", now=NOW))
        self.assertEqual(result["book"], fmt.LOVE)
        text = diary.read_for(private_session("10001"), now=NOW)["text"]
        self.assertIn("〔和10001〕", text)
        self.assertNotIn("Scheduler", text)

    def test_love_diary_invisible_in_group(self) -> None:
        """回归 P1-a：恋爱日记只对最亲密那个人的私聊可见。"""
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        run(diary.write_async(private_session("10001"), text="只对他说的一句话", now=NOW))
        run(diary.write_async(group_session(), text="群里写的普通一条", now=NOW))

        group_text = diary.read_for(group_session(), now=NOW)["text"]
        self.assertNotIn("只对他说的一句话", group_text)
        self.assertIn("群里写的普通一条", group_text)

        peer_text = diary.read_for(private_session("10001"), now=NOW)["text"]
        self.assertIn("只对他说的一句话", peer_text)

    def test_love_diary_invisible_to_other_private(self) -> None:
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        run(diary.write_async(private_session("10001"), text="秘密", now=NOW))
        text = diary.read_for(private_session("20002", "另一个人"), now=NOW)["text"]
        self.assertNotIn("秘密", text)

    def test_love_diary_visible_when_no_peers_configured(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(private_session("10001"), text="普通本子里的", now=NOW))
        self.assertIn("普通本子里的", diary.read_for(group_session(), now=NOW)["text"])

    def test_disabled_blocks_read_and_write(self) -> None:
        """回归 P2-b：开关关掉时**读也读不到**（原包只挡了写）。"""
        diary = make_diary(self.root, {"subsystems": {"diary": False}})
        self.assertFalse(diary.read_for(group_session(), now=NOW)["ok"])
        self.assertFalse(run(diary.write_async(group_session(), text="x", now=NOW))["ok"])
        self.assertEqual(diary.prompt_line(group_session(), now=NOW), "")
        self.assertFalse(run(diary.edit_for(group_session(), action="list", now=NOW))["ok"])

    def test_long_group_name_still_readable(self) -> None:
        """回归 P1-b（端到端）：超长群名 + 标注截断后，段数与内容都不能丢。"""
        diary = make_diary(self.root)
        long_name = "这是一个非常非常长的群名字" * 4
        run(diary.write_async(group_session(name=long_name), text="超长群名也要能读出来", now=NOW))
        result = diary.read_for(group_session(name=long_name), now=NOW)
        self.assertEqual(result["count"], 1)
        self.assertIn("超长群名也要能读出来", result["text"])
        self.assertEqual(len(diary.entries()), 1)

    def test_list_rewrite_delete_and_backup(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(group_session(), text="第一条", now=NOW))
        run(diary.write_async(group_session(), text="第二条", now=NOW))

        listing = run(diary.edit_for(group_session(), action="list", now=NOW))
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["count"], 2)
        self.assertTrue(all(seg["editable"] for seg in listing["segments"]))

        rewritten = run(
            diary.edit_for(group_session(), action="rewrite", match="第一条", text="改过的第一条", now=NOW)
        )
        self.assertTrue(rewritten["ok"])
        self.assertEqual(rewritten["seg"]["date"], "2026-10-04")
        self.assertIn("改过的第一条", diary.store.read(fmt.NORMAL))

        deleted = run(diary.edit_for(group_session(), action="delete", match="第二条", now=NOW))
        self.assertTrue(deleted["ok"])
        self.assertNotIn("第二条", diary.store.read(fmt.NORMAL))
        self.assertTrue(any(diary.layout.trash_dir.iterdir()))

    def test_old_entry_cannot_be_edited(self) -> None:
        diary = make_diary(self.root)
        storage.atomic_write_text(
            diary.layout.diary, "2026-01-01 10:00（平淡）〔群·甲〕\n很久以前的事。\n\n"
        )
        result = run(diary.edit_for(group_session(), action="rewrite", match="很久以前", text="想改掉", now=NOW))
        self.assertFalse(result["ok"])
        self.assertIn("太旧", result["error"])
        self.assertIn("很久以前的事。", diary.store.read(fmt.NORMAL))

    def test_future_entry_list_and_edit_agree(self) -> None:
        """回归 P3-b：未来日期的段，list 说不可改，edit 也不该改。"""
        diary = make_diary(self.root)
        storage.atomic_write_text(
            diary.layout.diary, "2026-10-20 21:30（期待）〔群·甲〕\n明天才发生的事。\n\n"
        )
        listing = run(diary.edit_for(group_session(), action="list", now=NOW))
        self.assertEqual(listing["segments"][0]["editable"], False)
        result = run(diary.edit_for(group_session(), action="rewrite", match="明天才发生", text="x", now=NOW))
        self.assertFalse(result["ok"])

    def test_edit_reports_missing_segment_with_hint(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(group_session(), text="只有这一条", now=NOW))
        result = run(diary.edit_for(group_session(), action="rewrite", match="根本没有这句", text="x", now=NOW))
        self.assertFalse(result["ok"])
        self.assertIn("没找到", result["error"])
        self.assertIn("hint", result)

    def test_search_and_filters(self) -> None:
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        run(diary.write_async(group_session(name="某某群"), text="群里说起了考试", now=NOW))
        run(diary.write_async(private_session("10001"), text="只对他说了考试的事", now=NOW))
        run(diary.write_async(private_session("20002", "另一个人"), text="和另一个人聊了天气", now=NOW))

        peer = private_session("10001")
        self.assertEqual(diary.read_for(peer, q="考试", now=NOW)["matched"], 2)
        scoped = diary.read_for(peer, q="考试", scope="group", now=NOW)
        self.assertEqual(scoped["count"], 1, "scope 过滤后的展示条数")
        self.assertEqual(scoped["matched"], 2, "matched 是关键词命中数（未按 scope 过滤）")
        self.assertEqual(diary.read_for(peer, q="考试", book="love", now=NOW)["matched"], 1)
        self.assertEqual(diary.read_for(peer, q="考试", book="normal", now=NOW)["matched"], 1)
        self.assertEqual(diary.read_for(peer, tail=1, now=NOW)["count"], 1)
        self.assertEqual(diary.read_for(peer, date="2026-10-04", now=NOW)["count"], 3)
        self.assertEqual(diary.read_for(peer, all_=True, now=NOW)["count"], 3)
        self.assertIn("没找到", diary.read_for(peer, q="不存在的词", now=NOW)["text"])

    def test_scope_note_mentions_unclassifiable_entries(self) -> None:
        diary = make_diary(self.root)
        storage.atomic_write_text(
            diary.layout.diary,
            "2026-10-01 10:00（平静）\n没有标注的旧条目\n\n2026-10-02 11:00〔群·甲〕\n有标注\n\n",
        )
        result = diary.read_for(group_session(), scope="group", now=NOW)
        self.assertEqual(result["count"], 1)
        self.assertIn("无法归类", result["text"])

    def test_prompt_line_states(self) -> None:
        empty = make_diary(self.root)
        self.assertIn("今天还没写", empty.prompt_line(group_session(), now=NOW))
        self.assertIn("本子上还什么都没有", empty.prompt_line(group_session(), now=NOW))

        day = datetime(2026, 10, 4, 12, 0, tzinfo=TZ)
        self.assertIn("想写随时", empty.prompt_line(group_session(), now=day))
        evening = datetime(2026, 10, 4, 20, 0, tzinfo=TZ)
        self.assertIn("有想记的就随手写", empty.prompt_line(group_session(), now=evening))
        self.assertIn("今天还空着", empty.prompt_line(group_session(), now=NOW))

        written = make_diary(self.root / "w")
        run(written.write_async(group_session(), text="今天写了", now=NOW))
        line = written.prompt_line(group_session(), now=NOW)
        self.assertIn("今天已写 1 段", line)
        self.assertIn("想再补两句", line)

    def test_prompt_line_early_hours_does_not_blame(self) -> None:
        """D8 回归（用户裁定"只改措辞"）：00:00~05:59 日历已经翻页、但她的一天还没开始，
        这时候不能说"今天还没写"（读起来像在怪她一整天没动笔），要说"新的一天刚开头"。

        这一改**只动措辞**：日期归属 / `day_of` / 配额一律照旧按日历日算，一个字节没变。
        """
        empty = make_diary(self.root)
        for hour, expected in ((0, "新的一天刚开头"), (3, "新的一天刚开头"),
                               (5, "新的一天刚开头"), (6, "今天还没写"),
                               (12, "今天还没写"), (21, "今天还没写")):
            moment = NOW.replace(hour=hour, minute=30)
            line = empty.prompt_line(group_session(), now=moment)
            self.assertIn(expected, line, f"{hour}:30 的措辞不对：{line.splitlines()[0]}")
            if hour < 6:
                self.assertNotIn("今天还没写", line, f"{hour}:30 不该出现「今天还没写」")

    def test_prompt_line_shows_previous_entry(self) -> None:
        diary = make_diary(self.root)
        yesterday = datetime(2026, 10, 3, 21, 0, tzinfo=TZ)
        run(diary.write_async(group_session(), text="昨天写的开头一句", now=yesterday))
        line = diary.prompt_line(group_session(), now=NOW)
        self.assertIn("上一条是 2026-10-03", line)

    def test_event_hint_needs_a_trigger_and_cools_down(self) -> None:
        diary = make_diary(self.root)
        session = group_session()
        self.assertEqual(diary.event_hint(session, now=NOW), "")
        strong = diary.event_hint(session, strong_text="记账命中", now=NOW)
        self.assertIn("diary_write", strong)
        self.assertEqual(diary.event_hint(session, strong_text="记账命中", now=NOW), "")

        # 冷却按会话共享一个时间戳：强提醒刚发过，弱提醒也会被压住（原包同款行为）
        self.assertEqual(diary.event_hint(session, exchange_who="某某", now=NOW), "")
        # 冷却按会话各自算：另一个会话不受这个会话的强提醒影响
        other = group_session(group_id="777", name="另一个群")
        weak = diary.event_hint(other, exchange_who="某某", now=NOW)
        self.assertIn("聊完一轮", weak)
        self.assertEqual(diary.event_hint(other, exchange_who="某某", now=NOW), "")
        self.assertIn(
            "聊完一轮",
            diary.event_hint(other, exchange_who="某某", now=NOW + timedelta(minutes=91)),
        )

    def test_event_hint_silent_when_already_written_today(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(group_session(), text="写过了", now=NOW))
        self.assertEqual(diary.event_hint(group_session(), strong_text="记账命中", now=NOW), "")

    def test_concurrent_appends_keep_both(self) -> None:
        diary = make_diary(self.root)

        async def scenario() -> None:
            await asyncio.gather(
                *(
                    diary.write_async(group_session(), text=f"并发第 {i} 条", now=NOW)
                    for i in range(5)
                )
            )

        run(scenario())
        entries = diary.entries()
        self.assertEqual(len(entries), 5)
        self.assertEqual(len({e.text for e in entries}), 5)

    def test_list_days_groups_by_date_and_hides_love(self) -> None:
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        run(diary.write_async(group_session(), text="群里一条", now=NOW))
        run(diary.write_async(private_session("10001"), text="只对他说", now=NOW))
        yesterday = datetime(2026, 10, 3, 20, 0, tzinfo=TZ)
        run(diary.write_async(group_session(), text="昨天一条", now=yesterday))

        peer_view = diary.list_days(private_session("10001"), limit=7)
        self.assertEqual(peer_view["count"], 2)
        self.assertEqual(peer_view["total"], 3)
        self.assertEqual(peer_view["days"][0]["date"], "2026-10-04")
        self.assertEqual(peer_view["days"][0]["entries"], 2)

        group_view = diary.list_days(group_session(), limit=7)
        self.assertEqual(group_view["total"], 2, "群聊看不到恋爱日记那本的段数")
        self.assertEqual(group_view["days"][0]["entries"], 1)
        self.assertFalse(group_view["days"][0].get("first") == "只对他说")

    def test_write_rejects_empty_body(self) -> None:
        diary = make_diary(self.root)
        result = run(diary.write_async(group_session(), text="   ", now=NOW))
        self.assertFalse(result["ok"])

    def test_write_clamps_to_max_chars(self) -> None:
        diary = make_diary(self.root, {"diary": {"max_chars": 200}})
        result = run(diary.write_async(group_session(), text="字" * 500, now=NOW))
        self.assertEqual(result["chars"], 200)


class ReadDayTest(TmpDirCase):
    """/看日记 图片卡的结构化读法：可见性与 read_for 同源，返回条目而不是文本。"""

    def test_returns_structured_entries_with_paragraphs(self) -> None:
        diary = make_diary(self.root)
        run(diary.write_async(private_session("10001"), text="第一段。\n\n第二段。", mood="开心", now=NOW))
        result = diary.read_day_for(private_session("10001"), date="2026-10-04")
        self.assertTrue(result["ok"])
        self.assertEqual(result["date"], "2026-10-04")
        self.assertEqual(result["count"], 1)
        entry = result["entries"][0]
        self.assertEqual(entry["mood"], "开心")
        self.assertEqual(entry["book"], fmt.NORMAL)
        self.assertEqual(entry["paragraphs"], ["第一段。", "第二段。"])

    def test_love_visibility_same_as_read_for(self) -> None:
        """/看日记 不开恋爱的口子：别人拿到 0 条，本人才拿得到。"""
        diary = make_diary(self.root, {"love_peers": ["10001"]})
        run(diary.write_async(private_session("10001"), text="秘密", now=NOW))
        other = diary.read_day_for(private_session("20002", "另一个人"), date="2026-10-04")
        self.assertEqual(other["count"], 0)
        self.assertEqual(other["entries"], [])
        peer = diary.read_day_for(private_session("10001"), date="2026-10-04")
        self.assertEqual(peer["count"], 1)
        self.assertEqual(peer["entries"][0]["book"], fmt.LOVE)

    def test_disabled_and_bad_date(self) -> None:
        diary = make_diary(self.root, {"subsystems": {"diary": False}})
        self.assertFalse(diary.read_day_for(private_session("10001"), date="2026-10-04")["ok"])
        diary2 = make_diary(self.root)
        self.assertFalse(diary2.read_day_for(private_session("10001"), date="2026-2-30")["ok"])


# ------------------------------------------------- 第 15 步：按段改删 / 回收站


TWO_SEG = (
    "2026-10-01 09:00（开心）〔和甲〕\n第一条。\n\n"
    "2026-10-03 21:30（难过）〔群·乙〕\n第二条。\n\n"
    "2026-10-04 08:00（平静）\n第三条。\n\n"
)


class SegmentIdentityTest(unittest.TestCase):
    """``seg_id`` 是面板改删的唯一定位方式：稳定 + 精确 + 会被内容变化顶掉。"""

    def test_id_is_stable_for_the_same_content(self) -> None:
        segs = fmt.split_segments(TWO_SEG)
        self.assertEqual(fmt.seg_id(segs[1]), fmt.seg_id(segs[1]))
        self.assertEqual(len(fmt.seg_id(segs[1])), fmt.SEG_ID_LEN)

    def test_id_is_stable_across_a_reparse(self) -> None:
        """同一段重新 parse 一次（等于文件没动）必须还是同一个 id。"""
        first = fmt.split_segments(TWO_SEG)
        second = fmt.split_segments(TWO_SEG)
        self.assertEqual([fmt.seg_id(s) for s in first], [fmt.seg_id(s) for s in second])

    def test_ids_differ_per_segment(self) -> None:
        ids = [fmt.seg_id(s) for s in fmt.split_segments(TWO_SEG)]
        self.assertEqual(len(set(ids)), len(ids))

    def test_editing_the_body_changes_the_id(self) -> None:
        """内容变了 id 就变——这是并发护栏，不是缺陷：文件被人手改过就该对不上。"""
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.rewrite_segment_at(TWO_SEG, seg, "改了。")
        self.assertTrue(result.ok)
        after = fmt.find_by_id(fmt.split_segments(result.text), fmt.seg_id(seg))
        self.assertIsNone(after)

    def test_find_by_id_is_exact_not_nearest(self) -> None:
        """按 id 找**不许**退化成"最后一段"——那是 ``match`` 那条路径的行为。"""
        segs = fmt.split_segments(TWO_SEG)
        self.assertIs(fmt.find_by_id(segs, fmt.seg_id(segs[0])), segs[0])
        self.assertIsNone(fmt.find_by_id(segs, "deadbeef00"))
        self.assertIsNone(fmt.find_by_id(segs, ""))
        self.assertIsNone(fmt.find_by_id(segs, None))


class RewriteBySegmentTest(unittest.TestCase):
    """按段改删：头行不动、段间空行还在、聊天侧那条路走同一份实现。"""

    def test_rewrite_keeps_the_head_line(self) -> None:
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.rewrite_segment_at(TWO_SEG, seg, "新正文。")
        self.assertTrue(result.ok)
        self.assertIn("2026-10-03 21:30（难过）〔群·乙〕\n新正文。", result.text)
        self.assertEqual(result.before, "第二条。")

    def test_rewrite_keeps_one_blank_line_between_segments(self) -> None:
        """回归 P1-b 那一类：正文整段替掉时**不能**把段间空行一起吃掉。"""
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.rewrite_segment_at(TWO_SEG, seg, "新正文。")
        self.assertIn("新正文。\n\n2026-10-04", result.text)
        self.assertEqual(len(fmt.split_segments(result.text)), 3)

    def test_rewrite_preserves_paragraphs(self) -> None:
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.rewrite_segment_at(TWO_SEG, seg, "第一段。\n\n第二段。")
        entries = fmt.parse_entries(result.text)
        self.assertEqual(entries[1].text, "第一段。\n\n第二段。")

    def test_rewrite_rejects_empty_body(self) -> None:
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.rewrite_segment_at(TWO_SEG, seg, "   ")
        self.assertFalse(result.ok)
        self.assertIn("删除", result.error)

    def test_delete_by_segment_removes_head_and_body(self) -> None:
        seg = fmt.split_segments(TWO_SEG)[1]
        result = fmt.delete_segment_at(TWO_SEG, seg)
        self.assertTrue(result.ok)
        self.assertNotIn("第二条。", result.text)
        self.assertNotIn("2026-10-03", result.text)
        self.assertEqual(len(fmt.split_segments(result.text)), 2)

    def test_match_path_shares_the_same_implementation(self) -> None:
        """``match`` 那条路（聊天工具）必须保持原有行为，包括"空正文提示用 delete"。"""
        result = fmt.rewrite_segment(
            TWO_SEG, match="第一条", new_body="X", now=NOW, within_days=7, tz=TZ
        )
        self.assertTrue(result.ok)
        self.assertIn("2026-10-01 09:00（开心）〔和甲〕\nX\n\n2026-10-03", result.text)
        empty = fmt.rewrite_segment(
            TWO_SEG, match="第一条", new_body=" ", now=NOW, within_days=7, tz=TZ
        )
        self.assertFalse(empty.ok)
        self.assertIn("delete", empty.error)


class InsertSegmentTest(unittest.TestCase):
    """回收站还原：插回**原本的时间位置**，不是甩到末尾。"""

    def _record(self, seg: fmt.Segment) -> dict:
        return {"date": seg.date, "time": seg.time, "mood": seg.mood,
                "who": seg.who, "text": seg.text}

    def test_round_trip_is_byte_identical(self) -> None:
        seg = fmt.split_segments(TWO_SEG)[1]
        gone = fmt.delete_segment_at(TWO_SEG, seg)
        back = fmt.insert_segment(gone.text, fmt.segment_from_record(self._record(seg)))
        self.assertEqual(back, TWO_SEG)

    def test_inserts_in_chronological_order(self) -> None:
        """删的是第一条，还原后它必须回到最前面，而不是追加到末尾。"""
        first = fmt.split_segments(TWO_SEG)[0]
        gone = fmt.delete_segment_at(TWO_SEG, first)
        back = fmt.insert_segment(gone.text, fmt.segment_from_record(self._record(first)))
        self.assertEqual(fmt.parse_entries(back)[0].date, "2026-10-01")

    def test_appends_when_it_is_the_newest(self) -> None:
        last = fmt.split_segments(TWO_SEG)[2]
        later = dict(self._record(last), date="2026-12-01", time="10:00")
        out = fmt.insert_segment(TWO_SEG, fmt.segment_from_record(later))
        self.assertEqual(fmt.parse_entries(out)[-1].date, "2026-12-01")

    def test_rebuilds_the_head_line_verbatim(self) -> None:
        """心情与标注要一起回来，不能还原成一个光秃秃的日期行。"""
        seg = fmt.split_segments(TWO_SEG)[1]
        gone = fmt.delete_segment_at(TWO_SEG, seg)
        back = fmt.insert_segment(gone.text, fmt.segment_from_record(self._record(seg)))
        self.assertIn("2026-10-03 21:30（难过）〔群·乙〕", back)


class AlreadyHappenedTest(unittest.TestCase):
    """未来段不管有没有豁免窗口都不可改——豁免的是"多久以前"，不是"没发生"。"""

    def _seg(self, stamp: str) -> fmt.Segment:
        date, clock = stamp.split(" ")
        return fmt.segment_from_record({"date": date, "time": clock, "text": "x"})

    def test_past_is_allowed(self) -> None:
        self.assertTrue(fmt.already_happened(self._seg("2026-10-01 09:00"), NOW, TZ))

    def test_future_is_rejected(self) -> None:
        self.assertFalse(fmt.already_happened(self._seg("2026-10-20 09:00"), NOW, TZ))

    def test_one_minute_clock_skew_is_tolerated(self) -> None:
        near = NOW - timedelta(seconds=30)
        seg = fmt.segment_from_record(
            {"date": near.strftime("%Y-%m-%d"), "time": near.strftime("%H:%M"), "text": "x"}
        )
        self.assertTrue(fmt.already_happened(seg, NOW, TZ))

    def test_unparsable_stamp_is_rejected(self) -> None:
        self.assertFalse(fmt.already_happened(self._seg("2026-10-01 99:99"), NOW, TZ))


class StoreApplyByIdTest(TmpDirCase):
    """文件层：按 id 改删、窗口判定、回收站归档与还原。"""

    def setUp(self) -> None:
        super().setUp()
        self.diary = make_diary(self.root)
        self.store = self.diary.store
        storage.atomic_write_text(self.diary.layout.diary, TWO_SEG)
        self.seeded = self.store.read(fmt.NORMAL)
        self.segs = fmt.split_segments(self.seeded)

    def _by_id(self, index: int) -> str:
        return fmt.seg_id(self.segs[index])

    def _seed_extra(self, head_line: str, body: str) -> str:
        """把一条**追加到文件末尾**并返回它的 id（id 必须从真实文件内容算）。"""
        storage.atomic_write_text(self.diary.layout.diary, self.store.read(fmt.NORMAL) + f"{head_line}\n{body}\n\n")
        return fmt.seg_id(fmt.split_segments(self.store.read(fmt.NORMAL))[-1])

    # ---- 改 / 删 ----

    def test_rewrite_by_id_hits_exactly_that_segment(self) -> None:
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=self._by_id(1), action="rewrite",
            new_body="改过的。", now=NOW, within_days=7, tz=TZ,
        ))
        self.assertTrue(result.ok)
        texts = [e.text for e in fmt.parse_entries(self.store.read(fmt.NORMAL))]
        self.assertEqual(texts, ["第一条。", "改过的。", "第三条。"])

    def test_unknown_id_is_reported_not_guessed(self) -> None:
        before = self.store.read(fmt.NORMAL)
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id="0123456789ab", action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertFalse(result.ok)
        self.assertIn("找不到", result.error)
        self.assertEqual(self.store.read(fmt.NORMAL), before)  # 一个字节都没动

    def test_bad_action_is_rejected(self) -> None:
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=self._by_id(0), action="archive",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertFalse(result.ok)

    def test_love_book_is_addressed_separately(self) -> None:
        """两本是两个文件：删恋爱日记那本，普通本一个字节都不动。"""
        love = make_diary(self.root, {"love_peers": ["10001"]})
        run(love.write_async(private_session("10001"), text="恋爱那条。", now=NOW))
        love_segs = fmt.split_segments(self.store.read(fmt.LOVE))
        result = run(self.store.apply_by_id(
            fmt.LOVE, seg_id=fmt.seg_id(love_segs[0]), action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertTrue(result.ok)
        self.assertIn("第一条。", self.store.read(fmt.NORMAL))  # 普通本没被动过
        self.assertEqual(fmt.split_segments(self.store.read(fmt.LOVE)), [])

    # ---- 窗口 ----

    def test_old_segment_is_rejected_inside_the_window(self) -> None:
        old_id = self._seed_extra("2026-06-01 09:00", "很久以前。")
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=old_id, action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertFalse(result.ok)
        self.assertIn("太旧", result.error)

    def test_window_error_points_at_the_switch(self) -> None:
        """窗口拒了要告诉主人"去哪打开"——否则他只会以为坏了。"""
        old_id = self._seed_extra("2026-06-01 09:00", "很久以前。")
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=old_id, action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertIn("面板不受可改天数限制", result.error)

    def test_zero_window_means_unlimited(self) -> None:
        old_id = self._seed_extra("2026-06-01 09:00", "很久以前。")
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=old_id, action="rewrite",
            new_body="豁免改的。", now=NOW, within_days=0, tz=TZ,
        ))
        self.assertTrue(result.ok)
        self.assertIn("豁免改的。", self.store.read(fmt.NORMAL))

    def test_future_segment_is_rejected_even_when_unlimited(self) -> None:
        future_id = self._seed_extra("2026-12-24 09:00", "还没发生。")
        result = run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=future_id, action="rewrite",
            new_body="x", now=NOW, within_days=0, tz=TZ,
        ))
        self.assertFalse(result.ok)
        self.assertIn("还没到", result.error)

    # ---- 回收站 ----

    def test_delete_archives_exactly_one_segment(self) -> None:
        run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=self._by_id(1), action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        items = self.store.trash_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "第二条。")
        self.assertEqual(items[0]["date"], "2026-10-03")
        self.assertEqual(items[0]["book"], fmt.NORMAL)

    def test_trash_uses_a_distinct_file_kind(self) -> None:
        """回收站是**单段**记录（seg-*.json）；聊天侧那份整本快照是另一类文件。"""
        run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=self._by_id(1), action="delete",
            now=NOW, within_days=7, tz=TZ,
        ))
        self.assertTrue(list(self.diary.layout.trash_dir.glob("seg-*.json")))
        self.assertTrue(list(self.diary.layout.trash_dir.glob("*-delete-normal.txt")))

    def test_rewrite_does_not_touch_the_trash(self) -> None:
        run(self.store.apply_by_id(
            fmt.NORMAL, seg_id=self._by_id(1), action="rewrite",
            new_body="改过的。", now=NOW, within_days=7, tz=TZ,
        ))
        self.assertEqual(self.store.trash_items(), [])

    def test_trash_items_are_newest_first(self) -> None:
        segs = fmt.split_segments(self.seeded)
        run(self.store.apply_by_id(fmt.NORMAL, seg_id=fmt.seg_id(segs[0]), action="delete",
                                    now=NOW, within_days=7, tz=TZ))
        run(self.store.apply_by_id(fmt.NORMAL, seg_id=fmt.seg_id(segs[2]), action="delete",
                                    now=NOW + timedelta(seconds=5), within_days=7, tz=TZ))
        items = self.store.trash_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["text"], "第三条。")   # 后删的在前面

    def test_trash_skips_broken_files(self) -> None:
        run(self.store.apply_by_id(fmt.NORMAL, seg_id=self._by_id(0), action="delete",
                                    now=NOW, within_days=7, tz=TZ))
        storage.atomic_write_text(self.diary.layout.trash_dir / "seg-broken.json", "{ 不是 json")
        storage.atomic_write_text(self.diary.layout.trash_dir / "seg-empty.json", "{}")
        self.assertEqual(len(self.store.trash_items()), 1)   # 坏的不让整页打不开

    def test_restore_puts_the_segment_back_in_place(self) -> None:
        run(self.store.apply_by_id(fmt.NORMAL, seg_id=self._by_id(0), action="delete",
                                    now=NOW, within_days=7, tz=TZ))
        trash_id = self.store.trash_items()[0]["id"]
        ok, why = run(self.store.restore_segment(trash_id, now=NOW))
        self.assertTrue(ok, why)
        self.assertEqual(self.store.read(fmt.NORMAL), self.seeded)
        self.assertEqual(self.store.trash_items(), [])   # 还原过就从回收站消失

    def test_restore_is_idempotent(self) -> None:
        run(self.store.apply_by_id(fmt.NORMAL, seg_id=self._by_id(1), action="delete",
                                    now=NOW, within_days=7, tz=TZ))
        trash_id = self.store.trash_items()[0]["id"]
        run(self.store.restore_segment(trash_id, now=NOW))
        # 归档文件被清掉了，这里手动放回去模拟"清归档失败"
        import json
        item = {"book": fmt.NORMAL, "date": "2026-10-03", "time": "21:30",
                "mood": "难过", "who": "群·乙", "text": "第二条。"}
        storage.atomic_write_text(
            self.diary.layout.trash_dir / f"{trash_id}.json", json.dumps(item, ensure_ascii=False)
        )
        ok, why = run(self.store.restore_segment(trash_id, now=NOW))
        self.assertFalse(ok)
        self.assertIn("已经在本子里", why)
        self.assertEqual(len(fmt.split_segments(self.store.read(fmt.NORMAL))), 3)  # 没插重

    def test_restore_rejects_unknown_id(self) -> None:
        ok, why = run(self.store.restore_segment("seg-不存在", now=NOW))
        self.assertFalse(ok)
        self.assertIn("找不到", why)

    def test_restore_rejects_path_traversal(self) -> None:
        """id 是前端回传的，必须钉死在回收站目录里。"""
        for evil in ("../../secrets", "..\\..\\secrets", "seg-x/../../y", "notseginit"):
            ok, _ = run(self.store.restore_segment(evil, now=NOW))
            self.assertFalse(ok, evil)

    def test_restore_skips_record_with_no_body(self) -> None:
        import json
        storage.atomic_write_text(
            self.diary.layout.trash_dir / "seg-nobody-normal.json",
            json.dumps({"book": fmt.NORMAL, "date": "2026-10-03", "time": "21:30", "text": ""}),
        )
        ok, why = run(self.store.restore_segment("seg-nobody-normal", now=NOW))
        self.assertFalse(ok)
        self.assertIn("没有正文", why)

    def test_trash_is_capped(self) -> None:
        from core.diary.store import TRASH_SEGMENT_CAP
        for index in range(TRASH_SEGMENT_CAP + 3):
            seg = fmt.split_segments(f"2026-10-04 08:{index % 60:02d}\n第 {index} 段。\n\n")
            storage.atomic_write_text(self.diary.layout.diary, f"2026-10-04 08:{index % 60:02d}\n第 {index} 段。\n\n")
            run(self.store.apply_by_id(
                fmt.NORMAL, seg_id=fmt.seg_id(seg[0]), action="delete",
                now=NOW + timedelta(seconds=index), within_days=7, tz=TZ,
            ))
        self.assertLessEqual(len(self.store.trash_items()), TRASH_SEGMENT_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
