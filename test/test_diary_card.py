"""日记卡的服务端渲染 + 面板预览（第 16 步）。

盯的是三件事：

1. **一份渲染器**：``/看日记`` 出图、面板卡片预览、``tools/preview_card.py`` 三个
   入口必须走**同一个** ``render_card``——否则预览和真卡片会长得不一样，那预览就是骗人的；
2. **模板只用支持的语法**：``core/diary/card.py`` 只认 ``{{ }}`` / ``{% for %}`` /
   ``{% if %}``，模板写了别的应该**抛异常**而不是静默出空图。这里把六款逐个真渲一遍。
3. **转义只在一处做**：出图那条链路没有 autoescape，``< > &`` 必须在
   ``card_context`` 里就变成实体——面板预览走同一份，看到的才是真效果。
"""

from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(_path))

from core import settings as settings_mod  # noqa: E402
from core import webui_data  # noqa: E402
from core.diary import card  # noqa: E402
from support import NOW, TmpDirCase  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")

SAMPLE = {
    "date": "2026-10-04",
    "weekday": "周日",
    "book_name": "日记",
    "count": 1,
    "entries": [
        {
            "time": "09:12",
            "mood": "安心",
            "who": "希",
            "paragraphs": ["第一段。", "第二段。"],
        }
    ],
}


class RenderSubsetTest(unittest.TestCase):
    def test_vars_loops_and_ifs(self) -> None:
        out = card.render("{{ a }}|{% for x in xs %}[{{ x }}]{% endfor %}|{% if a %}Y{% endif %}", {"a": "A", "xs": [1, 2]})
        self.assertEqual(out, "A|[1][2]|Y")

    def test_missing_key_is_empty_not_an_error(self) -> None:
        self.assertEqual(card.render("[{{ nope }}]", {}), "[]")

    def test_nested_lookup(self) -> None:
        self.assertEqual(card.render("{{ a.b.1 }}", {"a": {"b": ["x", "y"]}}), "y")

    def test_unsupported_syntax_raises(self) -> None:
        """宁可炸也不给空白预览——静默出空图会被当成"模板就这样"。"""
        with self.assertRaises(card.TemplateError):
            card.render("{% set x = 1 %}", {})
        with self.assertRaises(card.TemplateError):
            card.render("{{ name|upper }}", {"name": "x"})


class FontCssTest(unittest.TestCase):
    """``card.font_css`` 是原样内联的，白名单必须挡死。"""

    def test_empty_means_no_override(self) -> None:
        self.assertEqual(card.font_css(""), "")
        self.assertEqual(card.font_css(None), "")

    def test_font_stack(self) -> None:
        css = card.font_css("Noto Serif SC, serif")
        self.assertIn("font-family:Noto Serif SC, serif", css)
        self.assertIn("var(--card-font-stack)", css)

    def test_font_url_becomes_font_face(self) -> None:
        css = card.font_css("https://cdn.example.com/f/x.woff2")
        self.assertIn("@font-face", css)
        self.assertIn("https://cdn.example.com/f/x.woff2", css)

    def test_hostile_values_are_dropped(self) -> None:
        for evil in (
            "</style><script>alert(1)</script>",
            "javascript:alert(1)",
            "Noto Serif SC; } body{display:none",
            "http://evil/x.exe",
        ):
            self.assertEqual(card.font_css(evil), "", f"没挡住：{evil}")


class EveryStyleRendersTest(unittest.TestCase):
    """六款模板逐个真渲——少一款 / 用了不支持的语法，这里就炸。"""

    def test_all_declared_styles_render(self) -> None:
        for style in settings_mod.CARD_STYLE_CHOICES:
            html = card.render_card(style, SAMPLE)
            self.assertIn("<!DOCTYPE html>", html, style)
            self.assertIn("2026-10-04", html, style)
            self.assertIn("第一段。", html, style)
            self.assertNotIn("{{", html, f"{style} 渲完还有占位符")
            self.assertNotIn("{%", html, f"{style} 渲完还有控制符")

    def test_unknown_style_raises(self) -> None:
        with self.assertRaises(card.TemplateError):
            card.render_card("没有这一款", SAMPLE)


class CardContextTest(unittest.TestCase):
    """转义只在这一处做，所以这里就是唯一的防线。"""

    def test_escapes_body(self) -> None:
        ctx = card.card_context(
            date="2026-10-04", weekday="周日", book_name="日记",
            entries=[{"time": "09:12", "mood": "<开心>", "who": "A&B",
                      "paragraphs": ["<b>粗体</b> 与 & 符号"]}],
        )
        entry = ctx["entries"][0]
        self.assertEqual(entry["mood"], "&lt;开心&gt;")
        self.assertEqual(entry["who"], "A&amp;B")
        self.assertEqual(entry["paragraphs"][0], "&lt;b&gt;粗体&lt;/b&gt; 与 &amp; 符号")

    def test_font_css_carries_through(self) -> None:
        ctx = card.card_context(date="d", weekday="w", book_name="b", entries=[],
                                font_css=card.font_css("Noto Serif SC"))
        self.assertIn("Noto Serif SC", ctx["card_font_css"])


class CardPayloadTest(TmpDirCase):
    """``webui_data.diary_card_payload``：面板预览的数据面。"""

    def setUp(self) -> None:
        super().setUp()
        from core.diary.store import DiaryStore

        self.settings = settings_mod.load_settings({"timezone": "Asia/Shanghai"})
        self.layout = __import__("core.storage", fromlist=["storage"]).Layout(self.root).ensure()
        self.store = DiaryStore(layout=self.layout, locks=__import__(
            "core.storage", fromlist=["storage"]
        ).KeyedLocks())
        self.layout.trash_dir.mkdir(parents=True, exist_ok=True)
        (self.layout.diary).write_text(
            "2026-10-01 09:00（开心）〔和希#u_1001〕\n第一条。\n\n"
            "2026-10-03 21:30（难过）\n第二条。\n\n",
            encoding="utf-8",
        )

    def payload(self, **kwargs):
        return webui_data.diary_card_payload(
            diary_store=self.store, settings=self.settings, now=NOW, **kwargs
        )

    def test_renders_html_and_nav(self) -> None:
        data = self.payload()
        self.assertTrue(data["ok"])
        self.assertFalse(data["empty"])
        self.assertIn("<!DOCTYPE html>", data["html"])
        self.assertEqual(data["days"], ["2026-10-03", "2026-10-01"])   # 倒序：新的在前
        self.assertEqual(data["index"], 0)                              # 默认停在最新一天
        self.assertEqual(data["date"], "2026-10-03")
        self.assertEqual(data["count"], 1)
        self.assertEqual(list(data["styles"]), list(settings_mod.CARD_STYLE_CHOICES))

    def test_paging_by_date(self) -> None:
        data = self.payload(date="2026-10-01")
        self.assertEqual(data["date"], "2026-10-01")
        self.assertEqual(data["index"], 1)
        self.assertIn("第一条。", data["html"])

    def test_who_falls_back_to_tag_without_its_he_prefix(self) -> None:
        """标注是「和希」不是 id：raw_name 查不到也**不能把名字抹成空串**（P1 回归）。"""
        self.assertIn("和 希", self.payload(date="2026-10-01")["html"])

    def test_who_prefers_configured_name_by_person_id(self) -> None:
        """配了称呼就按 person_id 取**现用**称呼，不用头行里落盘那次的旧名。"""
        prefs = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "name_preference": {"u_1001": "小希"}}
        )
        data = webui_data.diary_card_payload(
            diary_store=self.store, settings=prefs, now=NOW, date="2026-10-01",
        )
        self.assertIn("和 小希", data["html"])
        self.assertNotIn("和 希<", data["html"])

    def test_unknown_date_falls_back_to_latest(self) -> None:
        self.assertEqual(self.payload(date="1999-01-01")["date"], "2026-10-03")

    def test_style_override_is_respected(self) -> None:
        self.assertEqual(self.payload(style="seal")["style"], "seal")

    def test_illegal_style_falls_back_to_default(self) -> None:
        self.assertEqual(self.payload(style="瞎写的")["style"], settings_mod.CARD_STYLE_DEFAULT)

    def test_empty_diary_is_not_an_error(self) -> None:
        self.layout.diary.write_text("", encoding="utf-8")
        data = self.payload()
        self.assertTrue(data["ok"])
        self.assertTrue(data["empty"])
        self.assertEqual(data["html"], "")

    def test_content_is_escaped_in_the_html(self) -> None:
        self.layout.diary.write_text(
            "2026-10-04 10:00\n写了个 <script>alert(1)</script>。\n\n", encoding="utf-8"
        )
        html = self.payload()["html"]
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()