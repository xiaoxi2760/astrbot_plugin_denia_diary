"""``core.imagery.prompt`` 的离线测试：确定性、要素拼装、长度上限、**不碰私有数据**。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.imagery import prompt as prompt_mod  # noqa: E402

SECRET = "私密测试字符串-不应出现在任何图像请求里"


class BuildSceneTest(unittest.TestCase):
    def test_all_parts_in_order(self) -> None:
        result = prompt_mod.build_scene(
            {
                "character_hint": "银发少女",
                "activity": {"title": "浇花", "location": "阳台"},
                "rhythm_word": "精神不错",
                "mood_word": "有点开心",
                "style_hint": "水彩",
            }
        )
        self.assertIn("银发少女", result["prompt"])
        self.assertIn("浇花", result["prompt"])
        self.assertIn("阳台", result["prompt"])
        self.assertIn("有点开心", result["prompt"])
        self.assertIn("水彩", result["prompt"])
        self.assertLess(
            result["prompt"].find("银发少女"), result["prompt"].find("浇花"), "人物在场景前"
        )
        self.assertLess(result["prompt"].find("浇花"), result["prompt"].find("水彩"), "场景在画风前")
        self.assertEqual(result["negative_prompt"], prompt_mod.DEFAULT_NEGATIVE_PROMPT)

    def test_activity_preferred_over_rhythm(self) -> None:
        result = prompt_mod.build_scene(
            {"activity": {"title": "看书", "location": ""}, "rhythm_word": "犯困"}
        )
        self.assertIn("看书", result["prompt"])
        self.assertNotIn("犯困", result["prompt"], "有当前活动时不再用作息词")

    def test_rhythm_used_when_no_activity(self) -> None:
        result = prompt_mod.build_scene({"rhythm_word": "晚饭后放松"})
        self.assertIn("晚饭后放松", result["prompt"])

    def test_fallback_when_nothing(self) -> None:
        result = prompt_mod.build_scene({})
        self.assertIn(prompt_mod.FALLBACK_PROMPT, result["prompt"])
        # 连兜底也不该把没给的东西编进去
        self.assertNotIn("心情", result["prompt"])

    def test_mood_is_truncated(self) -> None:
        word = "很" * 60
        result = prompt_mod.build_scene({"mood_word": word})
        self.assertIn("很" * prompt_mod.MOOD_MAX, result["prompt"])
        self.assertNotIn("很" * (prompt_mod.MOOD_MAX + 1), result["prompt"])

    def test_prompt_length_capped(self) -> None:
        result = prompt_mod.build_scene(
            {"character_hint": "长" * 300, "style_hint": "画" * 200, "mood_word": "心" * 24}
        )
        self.assertLessEqual(len(result["prompt"]), prompt_mod.PROMPT_MAX)

    def test_deterministic(self) -> None:
        spec = {"character_hint": "银发", "activity": {"title": "浇花", "location": "阳台"}, "mood_word": "平静"}
        self.assertEqual(prompt_mod.build_scene(spec), prompt_mod.build_scene(dict(spec)))

    def test_extra_keys_ignored(self) -> None:
        """只认公开日常的键：多塞的键（哪怕是测试的私密串）不进提示词。"""
        base = prompt_mod.build_scene({"character_hint": "银发少女"})
        extended = prompt_mod.build_scene({"character_hint": "银发少女", "diary_text": SECRET})
        self.assertEqual(base, extended)
        self.assertNotIn(SECRET, extended["prompt"])


if __name__ == "__main__":
    unittest.main()
