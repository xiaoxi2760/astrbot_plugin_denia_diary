"""``core.compose`` 的行为测试：优先级、预算裁剪、两个挂载点共用同一份渲染。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import compose  # noqa: E402
from support import TmpDirCase, group_session, make_diary  # noqa: E402


class _FakeDiary:
    def __init__(self, line: str = "", hint: str = "") -> None:
        self.line = line
        self.hint = hint
        self.calls: list[str] = []

    def prompt_line(self, session, *, now=None) -> str:
        self.calls.append("prompt_line")
        return self.line

    def event_hint(self, session, *, strong_text="", exchange_who="", now=None) -> str:
        self.calls.append("event_hint")
        return self.hint


class RenderTest(unittest.TestCase):
    def test_empty_sections_render_empty(self) -> None:
        self.assertEqual(compose.render([]), "")
        self.assertEqual(compose.render([compose.Section("a", "", 0)]), "")

    def test_higher_priority_comes_first(self) -> None:
        sections = [
            compose.Section("diary", "日记那行", compose.PRIORITY["diary"]),
            compose.Section("promise", "约定那句", compose.PRIORITY["promise"]),
        ]
        text = compose.render(sections)
        self.assertTrue(text.startswith("约定那句"), text)
        self.assertTrue(text.endswith("\n"))

    def test_low_priority_dropped_when_over_budget(self) -> None:
        sections = [
            compose.Section("promise", "P" * 30, compose.PRIORITY["promise"]),
            compose.Section("diary", "D" * 30, compose.PRIORITY["diary"]),
        ]
        text = compose.render(sections, budget=40)
        self.assertIn("P" * 30, text)
        self.assertNotIn("D" * 30, text, "低优先级整段被砍，而不是切一半")
        self.assertLessEqual(len(text.rstrip("\n")), 40)

    def test_first_section_is_truncated_if_it_alone_exceeds_budget(self) -> None:
        text = compose.render([compose.Section("promise", "P" * 100, 30)], budget=10)
        self.assertEqual(text, "P" * 10 + "\n")

    def test_whitespace_only_sections_are_skipped(self) -> None:
        text = compose.render(
            [
                compose.Section("promise", "\n\n", 30),
                compose.Section("diary", "留下我", 0),
            ]
        )
        self.assertEqual(text, "留下我\n")


class ComposePromptTest(TmpDirCase):
    def test_asks_diary_for_both_pieces(self) -> None:
        fake = _FakeDiary(line="日记那行", hint="提醒那句")
        text = compose.compose_prompt(fake, group_session())
        self.assertIn("提醒那句", text)
        self.assertIn("日记那行", text)
        self.assertEqual(fake.calls, ["event_hint", "prompt_line"])

    def test_empty_diary_renders_empty(self) -> None:
        self.assertEqual(compose.compose_prompt(_FakeDiary(), group_session()), "")

    def test_budget_is_respected_with_a_real_diary(self) -> None:
        diary = make_diary(self.root)
        text = compose.compose_prompt(diary, group_session())
        self.assertIn("【日记】", text)
        self.assertLessEqual(len(text.rstrip("\n")), compose.PROMPT_BUDGET)

    def test_disabled_diary_renders_empty(self) -> None:
        diary = make_diary(self.root, {"subsystems": {"diary": False}})
        self.assertEqual(compose.compose_prompt(diary, group_session()), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
