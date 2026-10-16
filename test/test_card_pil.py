"""PIL 出图引擎（第 20 步）的测试：字体探测、排版度量、折行、边界。

为什么 PIL 要有自己的测试文件：它是**唯一一条不依赖浏览器也不依赖网络**的路，也是降级梯的
最后一级。它坏掉的症状很隐蔽——不是报错，是**一张空白的或缺字的卡**被当成成功结果发出去。
所以这里盯的是"画出来的东西对不对"，不只是"没抛异常"。

真出图（Pillow + 字体都在）跑；没装 Pillow 或系统没中文字体的机器上相关用例自动 skip——
**绝不为跑测试去装依赖**。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402

from core.diary import card as card_mod  # noqa: E402
from core.diary import card_pil  # noqa: E402


def ctx(
    paragraphs: list[str] | None = None,
    *,
    mood: str = "安心",
    who: str = "和某某",
    date: str = "2026-10-13",
    page_index: int = 3,
    page_total: int = 9,
) -> dict:
    return card_mod.card_context(
        date=date,
        weekday="周二",
        book_name="普通日记",
        entries=[{"time": "20:14", "mood": mood, "who": who, "paragraphs": paragraphs or ["今天风不大。"]}],
        page_index=page_index,
        page_total=page_total,
    )


class PilAvailable(TmpDirCase):
    """PIL 能用时的基类；不能用就整体 skip。"""

    def setUp(self) -> None:
        super().setUp()
        ok, reason = card_pil.probe()
        if not ok:
            self.skipTest(f"这台机器不能 PIL 出图：{reason}")


class TestProbe(unittest.TestCase):
    def test_probe_never_raises(self) -> None:
        ok, reason = card_pil.probe()
        self.assertIsInstance(ok, bool)
        self.assertTrue(reason.strip(), "无论能不能，理由都要有——排查时全靠它")


class TestFontProbe(TmpDirCase):
    def test_env_override_wins_even_when_it_points_nowhere(self) -> None:
        """显式指定就该**以它为准**，哪怕文件不存在。

        静默回退到候选表才是坑：用户以为自己指定了某个字体，画出来的却是另一个，
        而日志里什么都看不出来。要改回自动探测，删掉这个环境变量即可。
        """
        saved = os.environ.get(card_pil.FONT_ENV)
        os.environ[card_pil.FONT_ENV] = str(Path("Z:/根本没有这个字体.ttf"))
        try:
            self.assertIsNone(card_pil.find_font())
            ok, reason = card_pil.probe()
            self.assertFalse(ok, "指定的字体不存在就该如实说不行，而不是悄悄换一个")
        finally:
            if saved is None:
                os.environ.pop(card_pil.FONT_ENV, None)
            else:
                os.environ[card_pil.FONT_ENV] = saved

    def test_env_override_to_a_real_font_is_used(self) -> None:
        saved = os.environ.get(card_pil.FONT_ENV)
        real = card_pil._find(card_pil._FONT_CANDIDATES)
        if real is None:
            self.skipTest("这台机器没有候选表里的字体")
        os.environ[card_pil.FONT_ENV] = str(real)
        try:
            self.assertEqual(card_pil.find_font(), real)
        finally:
            if saved is None:
                os.environ.pop(card_pil.FONT_ENV, None)
            else:
                os.environ[card_pil.FONT_ENV] = saved

    def test_no_font_at_all_reports_why(self) -> None:
        """一个字体都找不到时 probe 要说清是"没字体"而不是"没 Pillow"。"""
        saved_env = os.environ.pop(card_pil.FONT_ENV, None)
        saved_regular = card_pil._FONT_CANDIDATES
        saved_bold = card_pil._BOLD_CANDIDATES
        card_pil._FONT_CANDIDATES = ()
        card_pil._BOLD_CANDIDATES = ()
        try:
            self.assertIsNone(card_pil.find_font())
            self.assertIsNone(card_pil.find_bold_font())
            ok, reason = card_pil.probe()
            self.assertFalse(ok)
            self.assertIn(card_pil.FONT_ENV, reason, "理由里得告诉用户怎么手工指定")
            self.assertNotIn("Pillow", reason, "缺的是字体，别报成缺 Pillow")
        finally:
            card_pil._FONT_CANDIDATES = saved_regular
            card_pil._BOLD_CANDIDATES = saved_bold
            if saved_env is not None:
                os.environ[card_pil.FONT_ENV] = saved_env


class TestPenMetrics(PilAvailable):
    """折行与度量——画错这里就是"字挤出纸外"或"一行挤成两行"。"""

    def pen(self) -> card_pil._Pen:
        return card_pil._Pen(1)

    def test_chinese_wraps_by_character(self) -> None:
        pen = self.pen()
        lines = pen.wrap("今天风不大，傍晚去楼下走了两圈，买了点水果回来。", 16, 200)
        self.assertGreater(len(lines), 1)
        self.assertEqual("".join(lines), "今天风不大，傍晚去楼下走了两圈，买了点水果回来。")

    def test_latin_words_are_never_split(self) -> None:
        """日记里中英混排很常见——把 ``cafe`` 劈成 ca/fe 是最难看的排版错误。"""
        pen = self.pen()
        text = "今天去了 cafe 喝咖啡，天气不错。"
        lines = pen.wrap(text, 16, 150)
        self.assertTrue(any("cafe" in line for line in lines), "cafe 应当完整落在某一行")
        # 折行只该切位置，不该增删字符（行尾空格会被 rstrip 掉，所以比去掉空白的）
        self.assertEqual("".join(lines).replace(" ", ""), text.replace(" ", ""))

    def test_every_line_fits_the_width(self) -> None:
        pen = self.pen()
        text = "今天把窗户推开了一条缝，外面的风终于不冷了。傍晚去楼下走了两圈。"
        for line in pen.wrap(text, 16, 240):
            self.assertLessEqual(pen.width(line, 16), 240, f"这行超宽了：{line}")

    def test_empty_text_yields_one_empty_line(self) -> None:
        self.assertEqual(self.pen().wrap("", 16, 200), [""])

    def test_width_of_empty_text_is_zero(self) -> None:
        self.assertEqual(self.pen().width("", 16), 0)

    def test_scale_doubles_the_measurement(self) -> None:
        """超采样倍数只该放大尺寸，不该改变折行结果——否则 2 倍的图会排得不一样。"""
        one, two = card_pil._Pen(1), card_pil._Pen(2)
        text = "今天把窗户推开了一条缝，外面的风终于不冷了。"
        self.assertEqual(one.wrap(text, 16, 240), two.wrap(text, 16, 480))


class TestRender(PilAvailable):
    def render(self, data: dict, name: str = "card.png") -> Path:
        dest = self.root / name
        return card_pil.render_sync(data, dest)

    def test_produces_a_real_png(self) -> None:
        out = self.render(ctx())
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), "必须是真 PNG，不能是半张")
        self.assertGreater(len(raw), 3_000, f"PNG 小得可疑（{len(raw)} 字节），八成是空白页")

    def test_aspect_ratio_matches_the_other_engines(self) -> None:
        """画布宽度必须和 playwright / remote 对齐（800px），否则换引擎尺寸就变了。"""
        from PIL import Image

        out = self.render(ctx())
        with Image.open(out) as image:
            self.assertEqual(image.width, card_pil.CANVAS_WIDTH * card_pil.SCALE)

    def test_longer_diary_makes_a_taller_card(self) -> None:
        """高度必须跟着内容长——高度算错就是"内容被裁掉"或"大片空白"。"""
        from PIL import Image

        short = self.render(ctx(["一句话。"]), "short.png")
        long = self.render(ctx(["一段话。" * 40]), "long.png")
        with Image.open(short) as a, Image.open(long) as b:
            self.assertGreater(b.height, a.height)

    def test_entries_stack_vertically(self) -> None:
        """多条日记必须往下排，不能叠在一起。"""
        from PIL import Image

        one = self.render(ctx(), "one.png")
        two_data = card_mod.card_context(
            date="2026-10-13", weekday="周二", book_name="普通日记",
            entries=[
                {"time": "09:00", "mood": "安心", "who": "和某某", "paragraphs": ["早上。"]},
                {"time": "20:14", "mood": "安心", "who": "和某某", "paragraphs": ["晚上。"]},
            ],
        )
        two = self.render(two_data, "two.png")
        with Image.open(one) as a, Image.open(two) as b:
            self.assertGreater(b.height, a.height)

    def test_long_entry_grows_the_card(self) -> None:
        """单条很长时高度必须涨——固定高度就意味着文字被切掉。"""
        from PIL import Image

        short = self.render(ctx(["短。"]), "s.png")
        long = self.render(ctx(["很长的一段话，" * 30]), "l.png")
        with Image.open(short) as a, Image.open(long) as b:
            self.assertGreater(b.height, a.height * 1.5)

    def test_missing_mood_and_who_do_not_break(self) -> None:
        """旧段落可能没有心情/称呼——那两格就该空着，不该画崩。"""
        self.assertGreater(self.render(ctx(mood="", who="")).stat().st_size, 0)

    def test_empty_entry_still_produces_a_card(self) -> None:
        """空日记画得出来（只有抬头和页脚）——模板那边 ``{% for %}`` 本来就允许空。"""
        data = card_mod.card_context(
            date="2026-10-13", weekday="周二", book_name="普通日记", entries=[])
        self.assertGreater(self.render(data, "empty.png").stat().st_size, 0)

    def test_html_in_body_is_drawn_as_text_not_markup(self) -> None:
        """ctx 里已经 html 转义过了，PIL 只是画——别把 ``&lt;`` 当成要还原的实体。"""
        data = ctx(["她写了 <b>粗体</b> 与 & 符号。"])
        self.assertGreater(self.render(data, "esc.png").stat().st_size, 0)

    def test_creates_missing_parent_directory(self) -> None:
        dest = self.root / "nested" / "deep" / "card.png"
        self.assertEqual(card_pil.render_sync(ctx(), dest), dest)
        self.assertTrue(dest.is_file())

    def test_async_render_returns_the_path(self) -> None:
        dest = self.root / "async.png"
        self.assertEqual(asyncio.run(card_pil.render(ctx(), dest)), dest)
        self.assertTrue(dest.is_file())

    def test_render_without_a_font_raises_pil_error(self) -> None:
        """调用方只认 PilRenderError——漏了别的异常会把降级链在最底下断掉。"""
        saved = card_pil._FONT_CANDIDATES
        card_pil._FONT_CANDIDATES = ()
        try:
            with self.assertRaises(card_pil.PilRenderError):
                asyncio.run(card_pil.render(ctx(), self.root / "no.png"))
        finally:
            card_pil._FONT_CANDIDATES = saved


class TestPngIntegrity(PilAvailable):
    def test_image_can_be_reopened_and_is_well_formed(self) -> None:
        """PNG 有头没尾是很常见的坏法——重新打开一次就能验出来。"""
        from PIL import Image

        dest = self.root / "card.png"
        card_pil.render_sync(ctx(), dest)
        with Image.open(dest) as image:
            image.load()  # load 才真的解码；verify 只查结构
            self.assertEqual(image.format, "PNG")


if __name__ == "__main__":
    unittest.main()