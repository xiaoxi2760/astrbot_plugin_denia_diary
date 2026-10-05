"""第 5.1 步「出站文本清洗」的单测。

核两件事：

1. **纯函数**（``core/outbound.py``）——剥 / 不剥 / 剥空 / 坏正则 / 五种标记形态里**只剥**
   ``&&名字&&``；配置项（``core/settings.py``）的默认值与非法回落；
2. **钩子层**（``main.py`` 的 ``on_using_llm_tool``）——改的是**同一个 dict**、其它工具
   不受影响、非 plain 组件不动、内部异常时**原文照发**。
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402

from core import outbound  # noqa: E402
from core import settings as settings_mod  # noqa: E402

FIVE_FORMS = {
    "&&sleep&&": "&&名字&& 形态（要剥）",
    "[开心]": "[x] 形态（不动）",
    "(笑)": "(x) 形态（不动——会吃掉正常括号动作）",
    ":smile:": ":x: 形态（不动——会吃掉颜文字）",
    "meme:42": "meme:id 形态（不动）",
}


class TestStripMemeMarks(unittest.TestCase):
    def test_strips_and_trims_space(self) -> None:
        self.assertEqual(
            outbound.strip_meme_marks("……晚安，快睡吧。&&sleep&&"),
            "……晚安，快睡吧。",
        )
        self.assertEqual(outbound.strip_meme_marks("&&a&& 你好"), "你好")
        self.assertEqual(outbound.strip_meme_marks("你好 &&a&&"), "你好")

    def test_collapses_space_left_behind(self) -> None:
        """剥完留下的双空格压成一个，不留悬空空格。"""
        self.assertEqual(outbound.strip_meme_marks("他说 &&x&& 好"), "他说 好")
        self.assertEqual(outbound.strip_meme_marks("a &&x&& &&y&& b"), "a b")

    def test_keeps_newlines(self) -> None:
        """换行是消息的正常结构，不合并。"""
        self.assertEqual(outbound.strip_meme_marks("第一行 &&x&&\n第二行"), "第一行\n第二行")

    def test_blank_after_strip_keeps_original(self) -> None:
        """剥完为空 → 保持原文，绝不发空串。"""
        for text in ("&&a&&", " &&a&& ", "&&a&&&&b&&", "   &&a&&   "):
            self.assertEqual(outbound.strip_meme_marks(text), text, f"不该写回空串：{text!r}")

    def test_no_mark_untouched(self) -> None:
        self.assertEqual(outbound.strip_meme_marks("就是普通一句话。"), "就是普通一句话。")
        self.assertEqual(outbound.strip_meme_marks(""), "")

    def test_only_double_amp_form_is_stripped(self) -> None:
        """五种形态里只剥 ``&&名字&&``；宁可漏不可误伤。"""
        self.assertEqual(outbound.strip_meme_marks("[开心]"), "[开心]")
        self.assertEqual(outbound.strip_meme_marks("(笑)"), "(笑)")
        self.assertEqual(outbound.strip_meme_marks(":smile:"), ":smile:")
        self.assertEqual(outbound.strip_meme_marks("meme:42"), "meme:42")
        self.assertEqual(outbound.strip_meme_marks("a &&b&& c [d] (e) :f: meme:7"), "a c [d] (e) :f: meme:7")

    def test_form_five_shapes_present_in_marks(self) -> None:
        """把五种形态显式跑一遍，锁住"只剥一种"这条不变式。"""
        for text, label in FIVE_FORMS.items():
            # 前后各垫一个字：整条只有一个标记时按规则"剥空则保持原文"，测不出剥不剥
            cleaned = outbound.strip_meme_marks(f"前后{text}后")
            if text.startswith("&&"):
                self.assertEqual(cleaned, "前后后", label)
            else:
                self.assertEqual(cleaned, f"前后{text}后", label)
        # 整条只有一个标记的边界单独锁住
        self.assertEqual(outbound.strip_meme_marks("&&sleep&&"), "&&sleep&&")

    def test_bad_pattern_falls_back_and_never_raises(self) -> None:
        for bad in ("[bad(", "*", "(?P<x>x)(?P<x>y)", "", "   ", None, 123):
            self.assertEqual(
                outbound.strip_meme_marks("晚安 &&sleep&&", pattern=bad),  # type: ignore[arg-type]
                "晚安",
                f"坏正则 {bad!r} 应回落默认",
            )

    def test_zero_width_pattern_falls_back(self) -> None:
        """能匹配空串的正则会清空整条消息 → 配置层就该拒绝；这里也兜一层。"""
        self.assertEqual(
            outbound.strip_meme_marks("晚安 &&sleep&&", pattern="x*"),
            "晚安",
        )

    def test_custom_pattern(self) -> None:
        self.assertEqual(
            outbound.strip_meme_marks("晚安 [[sleep]]", pattern=r"\[\[([^&\s]{1,24})\]\]"),
            "晚安",
        )

    def test_has_meme_mark(self) -> None:
        self.assertTrue(outbound.has_meme_mark("a &&b&&"))
        self.assertFalse(outbound.has_meme_mark("a b"))
        self.assertFalse(outbound.has_meme_mark(""))


class TestCleanMessages(unittest.TestCase):
    def test_cleans_plain_only(self) -> None:
        messages = [
            {"type": "plain", "text": "晚安 &&sleep&&"},
            {"type": "image", "url": "&&not a mark&&"},
            {"type": "plain", "text": "早上好 &&rise&&"},
        ]
        out = outbound.clean_messages(messages)
        self.assertEqual(out[0]["text"], "晚安")
        self.assertEqual(out[1], {"type": "image", "url": "&&not a mark&&"})
        self.assertEqual(out[2]["text"], "早上好")

    def test_returns_same_object_when_nothing_changed(self) -> None:
        """没变化时返回**同一个**列表对象，钩子靠这个判断要不要写回。"""
        messages = [{"type": "plain", "text": "你好"}, {"type": "image", "url": "x"}]
        self.assertIs(outbound.clean_messages(messages), messages)

    def test_does_not_mutate_input_components(self) -> None:
        messages = [{"type": "plain", "text": "晚安 &&sleep&&"}]
        out = outbound.clean_messages(messages)
        self.assertEqual(messages[0]["text"], "晚安 &&sleep&&")  # 原组件没被动
        self.assertEqual(out[0]["text"], "晚安")

    def test_keeps_other_keys_of_plain(self) -> None:
        messages = [{"type": "plain", "text": "a &&x&&", "extra": 1}]
        self.assertEqual(outbound.clean_messages(messages)[0]["extra"], 1)

    def test_bad_shapes_returned_unchanged(self) -> None:
        for value in (None, "abc", 42, {}, ()):
            self.assertEqual(outbound.clean_messages(value), value)  # type: ignore[arg-type]
        self.assertEqual(outbound.clean_messages([]), [])

    def test_plain_only_mark_kept(self) -> None:
        """整条只有一个标记 → 剥空 → 保持原文（不写回空串）。"""
        messages = [{"type": "plain", "text": "&&sleep&&"}]
        self.assertIs(outbound.clean_messages(messages), messages)

    def test_object_component_is_copied_not_mutated(self) -> None:
        class Plain:
            def __init__(self, text: str) -> None:
                self.type = "plain"
                self.text = text

        component = Plain("晚安 &&sleep&&")
        out = outbound.clean_messages([component])
        self.assertIsNot(out[0], component)
        self.assertEqual(out[0].text, "晚安")
        self.assertEqual(component.text, "晚安 &&sleep&&")  # 原对象没被动

    def test_non_plain_object_untouched(self) -> None:
        class Image:
            type = "image"

            def __init__(self) -> None:
                self.url = "&&x&&"

        image = Image()
        self.assertIs(outbound.clean_messages([image])[0], image)


class TestOutboundSettings(unittest.TestCase):
    def test_defaults(self) -> None:
        config = settings_mod.load_settings({})
        self.assertTrue(config.outbound["strip_meme_marks"])
        self.assertEqual(config.outbound["marker_pattern"], outbound.DEFAULT_MARKER_PATTERN)
        self.assertIn("outbound", settings_mod.default_config())

    def test_explicit_values(self) -> None:
        config = settings_mod.load_settings(
            {"outbound": {"strip_meme_marks": False, "marker_pattern": r"@@[^@]+@@"}}
        )
        self.assertFalse(config.outbound["strip_meme_marks"])
        self.assertEqual(config.outbound["marker_pattern"], r"@@[^@]+@@")

    def test_illegal_pattern_falls_back(self) -> None:
        for bad in ("[bad(", "", "   ", "x*", 123, None):
            config = settings_mod.load_settings({"outbound": {"marker_pattern": bad}})
            self.assertEqual(
                config.outbound["marker_pattern"], outbound.DEFAULT_MARKER_PATTERN, f"应回落：{bad!r}"
            )

    def test_illegal_group_falls_back(self) -> None:
        config = settings_mod.load_settings({"outbound": "不是对象"})
        self.assertTrue(config.outbound["strip_meme_marks"])
        self.assertEqual(config.outbound["marker_pattern"], outbound.DEFAULT_MARKER_PATTERN)
        self.assertTrue(any("outbound" in w for w in config.warnings))

    def test_illegal_bool_falls_back(self) -> None:
        config = settings_mod.load_settings({"outbound": {"strip_meme_marks": "是"}})
        self.assertTrue(config.outbound["strip_meme_marks"])

    def test_core_has_no_astrbot_import(self) -> None:
        for name in ("outbound.py", "settings.py"):
            source = (Path(__file__).resolve().parents[1] / "core" / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    self.assertNotIn("astrbot", stripped, f"core/ 里不许 import astrbot：{name}")


class TestOutboundHook(TmpDirCase):
    """钩子层：确认"原地改同一个 dict"与"坏了不影响她说话"。"""

    @classmethod
    def setUpClass(cls) -> None:
        import test_main

        cls.module, cls.registry = test_main._load_plugin(Path(__file__).resolve().parents[1])

    def make_plugin(self, config: dict | None = None):
        target = self.root / "data"
        target.mkdir(parents=True, exist_ok=True)
        self.module.StarTools = sys.modules["astrbot.api.star"].StarTools
        sys.modules["astrbot.api.star"].StarTools.data_dir = target
        return self.module.DeniaDiary(context=SimpleNamespace(), config=config or {})

    def run_hook(self, plugin, tool_name: str, tool_args: dict, event=None):
        tool = SimpleNamespace(name=tool_name)
        if event is None:
            event = SimpleNamespace(get_extra=lambda key, default=None: default)
        asyncio.run(plugin.note_proactive_sent(event, tool, tool_args))
        return tool_args

    def test_mutates_the_same_dict_in_place(self) -> None:
        plugin = self.make_plugin()
        args = {"messages": [{"type": "plain", "text": "……晚安，快睡吧。&&sleep&&"}]}
        identity = id(args)
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(id(args), identity, "必须是同一个 dict（红线的核心）")
        self.assertEqual(args["messages"][0]["text"], "……晚安，快睡吧。")

    def test_cleans_passive_round_too(self) -> None:
        """被动轮（没有 cron extra）也要清洗——标记照样会漏给用户。"""
        plugin = self.make_plugin()
        args = {"messages": [{"type": "plain", "text": "早 &&rise&&"}]}
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["messages"][0]["text"], "早")

    def test_other_tools_untouched(self) -> None:
        plugin = self.make_plugin()
        for name in ("diary_write", "note_add", "mood_report", ""):
            args = {"messages": [{"type": "plain", "text": "留着 &&sleep&&"}]}
            self.run_hook(plugin, name, args)
            self.assertEqual(args["messages"][0]["text"], "留着 &&sleep&&", f"{name} 不该被动")

    def test_other_args_untouched(self) -> None:
        """只动 messages：同一个 dict 里的别的键一个都不许碰。"""
        plugin = self.make_plugin()
        args = {
            "messages": [{"type": "plain", "text": "晚安 &&sleep&&"}],
            "user_id": "10001",
            "note": "&&sleep&&",
        }
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["user_id"], "10001")
        self.assertEqual(args["note"], "&&sleep&&")

    def test_non_plain_components_untouched(self) -> None:
        plugin = self.make_plugin()
        messages = [
            {"type": "image", "url": "&&x&&"},
            {"type": "record", "path": "&&x&&"},
            {"type": "file", "path": "&&x&&"},
            {"type": "mention_user", "user_id": "&&x&&"},
        ]
        args = {"messages": messages}
        self.run_hook(plugin, "send_message_to_user", args)
        for item in args["messages"]:
            self.assertIn("&&x&&", "".join(str(v) for v in item.values()), f"非 plain 被动了：{item}")

    def test_switch_off(self) -> None:
        plugin = self.make_plugin({"outbound": {"strip_meme_marks": False}})
        args = {"messages": [{"type": "plain", "text": "晚安 &&sleep&&"}]}
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["messages"][0]["text"], "晚安 &&sleep&&")

    def test_custom_pattern_from_config(self) -> None:
        plugin = self.make_plugin({"outbound": {"marker_pattern": r"@@[^@]+@@"}})
        args = {"messages": [{"type": "plain", "text": "晚安 @@sleep@@ &&x&&"}]}
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["messages"][0]["text"], "晚安 &&x&&")  # 自定义正则不管 && 形态

    def test_internal_error_keeps_original_text(self) -> None:
        """红线：清洗内部炸了 → 原文照发，绝不连累她说话。"""
        plugin = self.make_plugin()
        original = [{"type": "plain", "text": "晚安 &&sleep&&"}]
        args = {"messages": original}

        class Boom:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("配置对象炸了")

        plugin.settings = SimpleNamespace(outbound=Boom())
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["messages"], original)  # 原样，包括原对象

    def test_missing_messages_key_is_safe(self) -> None:
        plugin = self.make_plugin()
        args: dict = {}
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args, {})

    def test_non_dict_args_is_safe(self) -> None:
        plugin = self.make_plugin()
        for value in (None, "abc", 42, []):
            self.run_hook(plugin, "send_message_to_user", value)  # type: ignore[arg-type]

    def test_single_mark_message_not_emptied(self) -> None:
        """整条只有一个标记时不发空串。"""
        plugin = self.make_plugin()
        args = {"messages": [{"type": "plain", "text": "&&sleep&&"}]}
        self.run_hook(plugin, "send_message_to_user", args)
        self.assertEqual(args["messages"][0]["text"], "&&sleep&&")

    def test_no_new_hook_registered(self) -> None:
        """清洗是加在现有钩子里，不是新加一个钩子。"""
        hooks = [h for h in self.registry["hooks"] if h[0] == "on_using_llm_tool"]
        self.assertEqual(len(hooks), 1, "只允许一个 on_using_llm_tool")


if __name__ == "__main__":
    unittest.main()
