"""小本本测试（第 2 步）。

对应第 2 步验收：① 事实型群聊读不到、该人私聊读得到；② 上限满了返回
"本子满了"且不报错、不写坏文件；③ 约定完成/过期语义；④ 并发 add + forget
不丢条目；⑤ subsystems.notebook=false 时读也读不到；⑥ 上限与口径的边界。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import compose  # noqa: E402
from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.notebook import Notebook, NotebookStore  # noqa: E402
from core.notebook.api import DISABLED, FULL_MESSAGE, NOT_FOUND  # noqa: E402
from support import NOW, TmpDirCase, group_session, private_session  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_notebook(root: Path, config: dict | None = None) -> Notebook:
    """造一个完整的小本本门面（配置 → 布局 → 文件层 → 门面）。"""
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    raw["diary"] = dict(raw.get("diary") or {})
    raw["notebook"] = dict(raw.get("notebook") or {})
    resolved = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    return Notebook(
        settings=resolved,
        layout=layout,
        store=NotebookStore(layout=layout, locks=locks),
        locks=locks,
    )


class StoreTest(TmpDirCase):
    def test_file_gets_schema_version_and_three_keys(self) -> None:
        nb = make_notebook(self.root)
        run(nb.note_add(private_session(), kind="fact", text="喜欢香菜", now=NOW))
        raw = json.loads(storage.read_text(nb.layout.notebook))
        self.assertEqual(raw["schema_version"], storage.SCHEMA_VERSION)
        self.assertIsInstance(raw["facts"], dict)
        self.assertIsInstance(raw["promises"], list)
        self.assertIsInstance(raw["trash"], list)
        self.assertEqual(raw["facts"]["10001"][0]["text"], "喜欢香菜")

    def test_add_fact_and_promise_roundtrip(self) -> None:
        nb = make_notebook(self.root)
        fact = run(nb.note_add(private_session(), kind="fact", text="喜欢香菜", now=NOW))
        promise = run(
            nb.note_add(
                private_session(),
                kind="promise",
                text="帮他查资料",
                due_at="2026-10-12",
                now=NOW,
            )
        )
        self.assertTrue(fact["ok"] and promise["ok"])
        doc = nb.store.read()
        self.assertEqual(len(doc["facts"]["10001"]), 1)
        self.assertEqual(len(doc["promises"]), 1)
        self.assertEqual(doc["promises"][0]["due_at"], "2026-10-12")
        self.assertIsNone(doc["promises"][0]["done_at"])
        self.assertNotEqual(fact["id"], promise["id"])
        self.assertEqual(fact["count"], 1)
        self.assertEqual(fact["limit"], 15)

    def test_fact_cap_rejects_without_corrupting_file(self) -> None:
        nb = make_notebook(self.root, {"notebook": {"fact_limit": 2}})
        run(nb.note_add(private_session(), kind="fact", text="第一条", now=NOW))
        run(nb.note_add(private_session(), kind="fact", text="第二条", now=NOW))
        full = run(nb.note_add(private_session(), kind="fact", text="第三条", now=NOW))
        self.assertFalse(full["ok"])
        self.assertEqual(full["error"], FULL_MESSAGE)
        doc = nb.store.read()
        self.assertEqual(len(doc["facts"]["10001"]), 2, "满了之后不多写")
        raw = json.loads(storage.read_text(nb.layout.notebook))
        self.assertEqual(len(raw["facts"]["10001"]), 2, "文件本身没有写坏")

    def test_promise_cap_counts_unfinished_only(self) -> None:
        nb = make_notebook(self.root, {"notebook": {"promise_limit": 1}})
        first = run(nb.note_add(private_session(), kind="promise", text="第一件", now=NOW))
        second = run(nb.note_add(private_session(), kind="promise", text="第二件", now=NOW))
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        done = run(nb.note_complete(private_session(), first["id"], now=NOW))
        self.assertTrue(done["ok"])
        third = run(nb.note_add(private_session(), kind="promise", text="第三件", now=NOW))
        self.assertTrue(third["ok"], "完成即腾坑")

    def test_forget_moves_to_trash_and_drops_oldest(self) -> None:
        nb = make_notebook(self.root, {"notebook": {"fact_limit": 100}})
        ids = [
            run(nb.note_add(private_session(), kind="fact", text=f"第 {i} 条", now=NOW))["id"]
            for i in range(51)
        ]
        for note_id in ids:
            result = run(nb.note_forget(private_session(), note_id, now=NOW))
            self.assertTrue(result["ok"])
        trash = nb.store.read()["trash"]
        self.assertEqual(len(trash), 50)
        self.assertEqual(trash[0]["entry"]["text"], "第 1 条", "最老的被挤出回收站")
        self.assertEqual(trash[-1]["entry"]["text"], "第 50 条")
        self.assertEqual(trash[0]["kind"], "fact")

    def test_read_normalizes_missing_keys(self) -> None:
        nb = make_notebook(self.root)
        storage.atomic_write_json(nb.layout.notebook, {"facts": {}})
        doc = nb.store.read()
        self.assertEqual(doc["promises"], [])
        self.assertEqual(doc["trash"], [])


class VisibilityTest(TmpDirCase):
    def test_fact_readable_in_private_only(self) -> None:
        """验收①：事实只在"与该人的私聊"读得到——群聊与别人的私聊都读不到。"""
        nb = make_notebook(self.root)
        run(nb.note_add(private_session("10001"), kind="fact", text="他的秘密", now=NOW))
        self.assertEqual([f["text"] for f in nb.facts_for(private_session("10001"))], ["他的秘密"])
        self.assertEqual(nb.facts_for(private_session("10002")), [])
        self.assertEqual(nb.facts_for(group_session(sender="10001")), [], "同一个群 ≠ 该人在场")

    def test_group_recorded_fact_reaches_person_private(self) -> None:
        """群里记的（about 缺省＝发言者），在该人的私聊注入得到，别处都注入不到。"""
        nb = make_notebook(self.root)
        result = run(
            nb.note_add(group_session(sender="10001"), kind="fact", text="爱吃香菜", now=NOW)
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["about"], "10001")
        self.assertIn("爱吃香菜", nb.fact_line(private_session("10001")))
        self.assertEqual(nb.fact_line(private_session("10002")), "")
        self.assertEqual(nb.fact_line(group_session(sender="10001")), "", "群里一律不念")

    def test_fact_line_private_shows_content(self) -> None:
        nb = make_notebook(self.root)
        self.assertEqual(nb.fact_line(private_session("10001"), now=NOW), "")
        run(nb.note_add(private_session("10001"), kind="fact", text="生日在 3 月", now=NOW))
        line = nb.fact_line(private_session("10001"), now=NOW)
        self.assertIn("【小本本】", line)
        self.assertIn("生日在 3 月", line)

    def test_promise_line_private_shows_text_and_due(self) -> None:
        nb = make_notebook(self.root)
        run(
            nb.note_add(
                private_session("10001"),
                kind="promise",
                text="帮他查资料",
                due_at="2026-10-12",
                now=NOW,
            )
        )
        line = nb.promise_line(private_session("10001"), now=NOW)
        self.assertIn("【小本本】你答应过的事", line)
        self.assertIn("帮他查资料", line)
        self.assertIn("2026-10-12", line)

    def test_promise_line_group_is_desensitized_count(self) -> None:
        """群聊只出脱敏计数行：不带任何一条内容。"""
        nb = make_notebook(self.root)
        run(nb.note_add(private_session("10001"), kind="promise", text="给他带书", now=NOW))
        run(nb.note_add(private_session("10002"), kind="promise", text="陪她复盘", now=NOW))
        line = nb.promise_line(group_session(sender="10003"), now=NOW)
        self.assertIn("2 件", line)
        self.assertNotIn("带书", line)
        self.assertNotIn("复盘", line)

    def test_promise_line_group_silent_when_nothing_open(self) -> None:
        nb = make_notebook(self.root)
        self.assertEqual(nb.promise_line(group_session(), now=NOW), "")

    def test_overdue_promises_first_with_marker(self) -> None:
        nb = make_notebook(self.root)
        run(nb.note_add(private_session(), kind="promise", text="没期限的事", now=NOW))
        run(
            nb.note_add(
                private_session(), kind="promise", text="将来的事", due_at="2026-10-12", now=NOW
            )
        )
        run(
            nb.note_add(
                private_session(), kind="promise", text="过期的事", due_at="2026-10-01", now=NOW
            )
        )
        line = nb.promise_line(private_session(), now=NOW)
        lines = [part for part in line.split("\n") if part.startswith("- ")]
        self.assertEqual(len(lines), 3)
        self.assertIn("过期的事", lines[0])
        self.assertIn("已过期 2026-10-01", lines[0])
        self.assertIn("将来的事", lines[1])
        self.assertIn("2026-10-12", lines[1])
        self.assertIn("没期限的事", lines[2])
        self.assertNotIn("已过期", lines[2])

    def test_promises_due_window(self) -> None:
        """验收③（到期口径）：到期与过期算 due，未来与完成不算。"""
        nb = make_notebook(self.root)
        run(
            nb.note_add(
                private_session(), kind="promise", text="昨天到期", due_at="2026-10-03", now=NOW
            )
        )
        run(
            nb.note_add(
                private_session(), kind="promise", text="今天到期", due_at="2026-10-04", now=NOW
            )
        )
        run(
            nb.note_add(
                private_session(), kind="promise", text="明天到期", due_at="2026-10-05", now=NOW
            )
        )
        no_due = run(nb.note_add(private_session(), kind="promise", text="没期限", now=NOW))
        due = nb.promises_due(now=NOW)
        self.assertEqual([item["text"] for item in due], ["昨天到期", "今天到期"])
        run(nb.note_complete(private_session(), due[0]["id"], now=NOW))
        due_after = nb.promises_due(now=NOW)
        self.assertEqual([item["text"] for item in due_after], ["今天到期"])
        self.assertNotIn(no_due["id"], [item["id"] for item in due_after])


class MutationTest(TmpDirCase):
    def test_complete_sets_done_at_and_stops_injection(self) -> None:
        nb = make_notebook(self.root)
        added = run(
            nb.note_add(
                private_session("10001"), kind="promise", text="帮他查资料", now=NOW
            )
        )
        result = run(nb.note_complete(private_session("10001"), added["id"], now=NOW))
        self.assertTrue(result["ok"])
        doc = nb.store.read()
        self.assertTrue(doc["promises"][0]["done_at"])
        self.assertNotIn("帮他查资料", nb.promise_line(private_session("10001"), now=NOW))

    def test_complete_requires_ownership(self) -> None:
        """改动按条目归属判定：about == 眼前人 才能动，私聊群聊同一套。"""
        nb = make_notebook(self.root)
        added = run(nb.note_add(private_session("10001"), kind="promise", text="给他带书", now=NOW))
        self.assertFalse(run(nb.note_complete(private_session("10002"), added["id"], now=NOW))["ok"])
        self.assertEqual(
            run(nb.note_complete(group_session(sender="10002"), added["id"], now=NOW))["error"],
            NOT_FOUND,
        )
        self.assertTrue(
            run(nb.note_complete(group_session(sender="10001"), added["id"], now=NOW))["ok"],
            "群聊里眼前人正是这条约定关于的人，允许销账",
        )

    def test_complete_rejects_fact_and_unknown_ids(self) -> None:
        nb = make_notebook(self.root)
        fact = run(nb.note_add(private_session(), kind="fact", text="喜欢香菜", now=NOW))
        result = run(nb.note_complete(private_session(), fact["id"], now=NOW))
        self.assertFalse(result["ok"])
        self.assertIn("事实", result["error"], "事实没有完成语义")
        self.assertIn("note_forget", result["error"])
        unknown = run(nb.note_complete(private_session(), "nope", now=NOW))
        self.assertEqual(unknown["error"], NOT_FOUND)

    def test_complete_already_done(self) -> None:
        nb = make_notebook(self.root)
        added = run(nb.note_add(private_session(), kind="promise", text="陪他复盘", now=NOW))
        run(nb.note_complete(private_session(), added["id"], now=NOW))
        again = run(nb.note_complete(private_session(), added["id"], now=NOW))
        self.assertFalse(again["ok"])
        self.assertIn("早就完成", again["error"])

    def test_forget_removes_and_requires_ownership(self) -> None:
        nb = make_notebook(self.root)
        fact = run(nb.note_add(private_session("10001"), kind="fact", text="他的秘密", now=NOW))
        self.assertEqual(
            run(nb.note_forget(private_session("10002"), fact["id"], now=NOW))["error"], NOT_FOUND
        )
        result = run(nb.note_forget(private_session("10001"), fact["id"], now=NOW))
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "fact")
        self.assertEqual(nb.facts_for(private_session("10001")), [])
        trash = nb.store.read()["trash"]
        self.assertEqual(len(trash), 1)
        self.assertEqual(trash[0]["entry"]["id"], fact["id"])

    def test_forget_promise_goes_to_trash(self) -> None:
        nb = make_notebook(self.root)
        promise = run(nb.note_add(private_session(), kind="promise", text="带书", now=NOW))
        result = run(nb.note_forget(private_session(), promise["id"], now=NOW))
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "promise")
        self.assertEqual(nb.store.read()["promises"], [])

    def test_add_rejects_bad_kind_text_and_due(self) -> None:
        nb = make_notebook(self.root)
        self.assertIn("只记两类", run(nb.note_add(private_session(), kind="joke", text="x", now=NOW))["error"])
        self.assertIn("空", run(nb.note_add(private_session(), kind="fact", text="  ", now=NOW))["error"])
        bad_due = run(
            nb.note_add(private_session(), kind="promise", text="x", due_at="10月12日", now=NOW)
        )
        self.assertIn("YYYY-MM-DD", bad_due["error"])

    def test_add_in_group_defaults_to_sender(self) -> None:
        nb = make_notebook(self.root)
        result = run(
            nb.note_add(group_session(group_id="555", sender="20002"), kind="promise", text="下周一起吃饭", now=NOW)
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["about"], "20002")
        self.assertIn("一起吃饭", nb.promise_line(private_session("20002"), now=NOW))

    def test_add_fails_when_person_unknown(self) -> None:
        from core.session import Session

        nb = make_notebook(self.root)
        result = run(nb.note_add(Session(), kind="fact", text="没人", now=NOW))
        self.assertFalse(result["ok"])
        self.assertIn("记给谁", result["error"])

    def test_note_list_private_shows_ids_and_counts(self) -> None:
        nb = make_notebook(self.root, {"notebook": {"fact_limit": 2, "promise_limit": 5}})
        run(nb.note_add(private_session("10001"), kind="fact", text="喜欢香菜", now=NOW))
        promise = run(
            nb.note_add(
                private_session("10001"), kind="promise", text="帮他查资料", due_at="2026-10-12", now=NOW
            )
        )
        run(nb.note_complete(private_session("10001"), promise["id"], now=NOW))
        result = nb.note_list(private_session("10001"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["scope"], "private")
        self.assertEqual(len(result["facts"]), 1)
        self.assertEqual(len(result["promises"]), 1)
        self.assertEqual(result["facts"][0]["text"], "喜欢香菜")
        self.assertTrue(result["promises"][0]["done_at"], "完成的约定也列出来（带完成标记）")
        self.assertTrue(result["promises"][0]["id"])
        self.assertEqual(result["limits"], {"promise_limit": 5, "fact_limit": 2})

    def test_note_list_group_reports_count_only(self) -> None:
        nb = make_notebook(self.root)
        run(nb.note_add(private_session("10001"), kind="fact", text="秘密", now=NOW))
        run(nb.note_add(private_session("10001"), kind="promise", text="开着的事", now=NOW))
        result = nb.note_list(group_session(sender="10003"))
        self.assertEqual(result["scope"], "group")
        self.assertEqual(result["open_count"], 1)
        self.assertNotIn("秘密", str(result))
        self.assertNotIn("开着的事", str(result))

    def test_note_list_rejects_unknown_kind(self) -> None:
        nb = make_notebook(self.root)
        self.assertFalse(nb.note_list(private_session(), kind="banana")["ok"])
        self.assertTrue(nb.note_list(private_session(), kind="fact")["ok"])
        self.assertTrue(nb.note_list(private_session(), kind="约定")["ok"])


class DisabledTest(TmpDirCase):
    """验收⑤：subsystems.notebook=false 时写不进、读也读不到。"""

    def _disabled(self) -> Notebook:
        return make_notebook(self.root, {"subsystems": {"notebook": False}})

    def test_reads_are_empty(self) -> None:
        nb = self._disabled()
        self.assertEqual(nb.facts_for(private_session()), [])
        self.assertEqual(nb.promises_for(private_session(), now=NOW), [])
        self.assertEqual(nb.promises_due(now=NOW), [])
        self.assertEqual(nb.fact_line(private_session(), now=NOW), "")
        self.assertEqual(nb.promise_line(private_session(), now=NOW), "")
        self.assertEqual(nb.promise_line(group_session(), now=NOW), "")

    def test_writes_are_rejected(self) -> None:
        nb = self._disabled()
        for result in (
            run(nb.note_add(private_session(), kind="fact", text="x", now=NOW)),
            run(nb.note_complete(private_session(), "whatever", now=NOW)),
            run(nb.note_forget(private_session(), "whatever", now=NOW)),
        ):
            self.assertEqual(result["error"], DISABLED)
        self.assertEqual(nb.note_list(private_session())["error"], DISABLED)

    def test_data_untouched_when_disabled(self) -> None:
        enabled = make_notebook(self.root)
        run(enabled.note_add(private_session(), kind="fact", text="留着", now=NOW))
        disabled = make_notebook(self.root, {"subsystems": {"notebook": False}})
        self.assertEqual(disabled.facts_for(private_session()), [])
        self.assertIn("留着", storage.read_text(self.root / "notebook.json"), "数据保留")


class ConcurrencyTest(TmpDirCase):
    def test_concurrent_adds_and_forget_keep_entries(self) -> None:
        """验收④：并发 add + forget 不丢条目。"""
        nb = make_notebook(self.root)
        seed = run(nb.note_add(private_session(), kind="fact", text="老条目", now=NOW))

        async def scenario():
            return await asyncio.gather(
                nb.note_forget(private_session(), seed["id"], now=NOW),
                *(
                    nb.note_add(private_session(), kind="fact", text=f"并发第 {i} 条", now=NOW)
                    for i in range(5)
                ),
            )

        results = run(scenario())
        self.assertTrue(all(item["ok"] for item in results[1:]), "5 条并发新增都成功")
        facts = nb.facts_for(private_session())
        texts = {item["text"] for item in facts}
        self.assertEqual(len(facts), 5)
        self.assertEqual(len(texts), 5, "没有互相覆盖")
        self.assertNotIn("老条目", texts)
        trash = nb.store.read()["trash"]
        self.assertEqual([item["entry"]["id"] for item in trash], [seed["id"]])

    def test_concurrent_adds_never_exceed_cap(self) -> None:
        nb = make_notebook(self.root, {"notebook": {"fact_limit": 3}})

        async def scenario():
            return await asyncio.gather(
                *(
                    nb.note_add(private_session(), kind="fact", text=f"抢坑第 {i} 条", now=NOW)
                    for i in range(6)
                )
            )

        results = run(scenario())
        ok = [item for item in results if item["ok"]]
        full = [item for item in results if not item["ok"]]
        self.assertEqual(len(ok), 3, "上限检查在锁内：并发抢坑只进 3 条")
        self.assertEqual(len(full), 3)
        for item in full:
            self.assertEqual(item["error"], FULL_MESSAGE)
        self.assertEqual(len(nb.facts_for(private_session())), 3)


class SettingsNotebookTest(unittest.TestCase):
    def test_defaults_are_20_and_15(self) -> None:
        config = settings_mod.default_config()
        self.assertEqual(config["notebook"], {"promise_limit": 20, "fact_limit": 15})

    def test_out_of_range_is_clamped_and_reported(self) -> None:
        result = settings_mod.load_settings({"notebook": {"promise_limit": 0, "fact_limit": 500}})
        self.assertEqual(result.notebook["promise_limit"], 1)
        self.assertEqual(result.notebook["fact_limit"], 200)
        self.assertEqual(len(result.warnings), 2)

    def test_wrong_type_falls_back(self) -> None:
        result = settings_mod.load_settings({"notebook": {"promise_limit": "很多"}})
        self.assertEqual(result.notebook["promise_limit"], 20)
        self.assertTrue(any("promise_limit" in item for item in result.warnings))


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


class ComposeIntegrationTest(TmpDirCase):
    def test_without_notebook_output_matches_step1(self) -> None:
        """保险丝：不传 notebook 时输出与第 1 步逐字节一致，两个挂载点不动。"""
        diary = _FakeDiary(line="日记那行", hint="提醒那句")
        session = private_session()
        baseline = compose.render(
            [
                compose.Section("reminder", "提醒那句", compose.PRIORITY["reminder"]),
                compose.Section("diary", "日记那行", compose.PRIORITY["diary"]),
            ]
        )
        self.assertEqual(compose.compose_prompt(diary, session), baseline)
        self.assertEqual(compose.compose_prompt(diary, session, notebook=None), baseline)
        self.assertNotIn("小本本", baseline)

    def test_promise_and_fact_precede_diary(self) -> None:
        diary = _FakeDiary(line="日记那行")
        notebook = _FakeNotebook(promise="约定那段", fact="事实那段")
        text = compose.compose_prompt(diary, private_session(), notebook=notebook)
        self.assertTrue(text.startswith("约定那段"), text)
        self.assertIn("事实那段", text)
        self.assertLess(text.index("事实那段"), text.index("日记那行"))

    def test_budget_cuts_from_diary_first(self) -> None:
        diary = _FakeDiary(line="日记那行" * 75)
        notebook = _FakeNotebook(promise="约定那段" * 40, fact="事实那段")
        text = compose.compose_prompt(
            diary, private_session(), notebook=notebook, budget=compose.PROMPT_BUDGET
        )
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)
        self.assertIn("约定那段", text, "约定（不可省）最先保留")
        self.assertIn("事实那段", text)
        self.assertNotIn("日记那行", text, "超预算从低优先级开始砍")


if __name__ == "__main__":
    unittest.main(verbosity=2)
