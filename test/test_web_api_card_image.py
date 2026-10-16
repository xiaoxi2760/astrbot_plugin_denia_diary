"""``diary/card-image``（导出为图片）的 handler 层测试（第 19 步）。

单独一个文件，因为这条路由和别的都不一样：它是全插件**唯一一条要真出图**的 WebUI 路由。
别的 handler 都是纯本地计算，这个要过出图链路，一旦出错（引擎全挂、读不到文件）用户就是
"点导出什么都没发生"。

所以这里把三条路都钉住：

* 正常 → 200 + ``{ok, data_url, filename, bytes}``，data_url 能解回原始字节；
* 这本没日记 → 200 + ``{ok, empty}``（前端据此提示，不当成失败）；
* 出图链路挂了 → **500 + 可读原因**。这里必须跟"业务失败 200"分开：导出失败
  没有任何业务语义，是环境问题；再糊成 200 会让前端以为只是"这天没内容"。

桩照 ``test_web_api_diary.py`` 那一套：同一个 ``web_api._web`` 离线桩，不另造一个宽容的。

第 20 步起导出与 ``/看日记`` 共用 ``core.diary.card_render.render_html``，所以桩里的
``render_card_html`` 直接绑真函数、只把"远端出图"换成假的——这样"导出用的是同一份
RENDER_OPTIONS / 同一条降级链"这条断言才是真的在验东西，而不是验桩自己。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.diary import card as card_mod  # noqa: E402
from core.diary import card_render  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)

SEED = "2026-10-04 20:00（难过）\n新的一条。\n\n"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
"""不是真 PNG，只要 handler 原样 base64 回去就够验数据没被改。"""


class _Engines:
    """把 ``card_render`` 里的引擎换成假实现（只在这一轮测试里生效）。

    这台机器上 playwright 与 pil **都是真的能用的**，不换掉就测不到"导出失败"——
    测到的只会是"某条路正好能用"。替换的是**函数**不是整个模块：
    ``render_pil`` 里要用 ``card_pil.PilRenderError``，替身得留着那个类。
    """

    def __init__(self, *names: str) -> None:
        self.names = names or tuple(card_render.ENGINE_ORDER)
        self._saved: dict = {}

    def __enter__(self):
        async def dead(*args, **kwargs):
            return None

        for name in self.names:
            self._saved[name] = getattr(card_render, f"render_{name}")
            setattr(card_render, f"render_{name}", dead)
        return self

    def __exit__(self, *exc):
        for name, original in self._saved.items():
            setattr(card_render, f"render_{name}", original)
        self._saved.clear()
        return False


def _all_engines_dead() -> _Engines:
    return _Engines()


def _only_engine(name: str) -> _Engines:
    return _Engines(*[n for n in card_render.ENGINE_ORDER if n != name])


async def _async_value(value):
    return value


class CardImageHandlerCase(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers, routes

        self.handlers = handlers
        self.routes = routes
        self.layout = storage.Layout(self.root).ensure()
        self.diary_store = DiaryStore(layout=self.layout, locks=storage.KeyedLocks())
        storage.atomic_write_text(self.layout.diary, SEED)

        self.render_calls: list[dict] = []
        self.render_should_fail = False
        self.rendered_file = self.root / "card.png"
        self.rendered_file.write_bytes(PNG_BYTES)

        async def html_render(template, data, return_url=False, options=None):
            self.render_calls.append(
                {"template": template, "data": data, "return_url": return_url, "options": options}
            )
            if self.render_should_fail:
                raise RuntimeError("转图端点连不上")
            return str(self.rendered_file)

        # 离线跑不了本地出图（这台机器有没有浏览器不是测试该关心的），所以把 engine 钉在
        # remote：真函数照跑，只是"本地那条"被配置摘掉了，验的是**编排本身**。
        self.deps = SimpleNamespace(
            settings=settings_mod.load_settings({"timezone": "Asia/Shanghai"}),
            layout=self.layout,
            diary=SimpleNamespace(store=self.diary_store),
            proactive=SimpleNamespace(store=SimpleNamespace(contacts=lambda: {})),
            render_card_html=lambda html, ctx, key: card_render.render_html(
                html,
                ctx,
                cache_dir=self.layout.card_cache,
                key=key,
                engine="remote",
                remote=html_render,
            ),
            html_render=html_render,
            _now=lambda: NOW,
        )

    def call(self, name: str, *, query=None):
        fake = SimpleNamespace(
            query=query or {},
            username="admin",
            json=lambda default=None: _async_value(default),
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)[name]
            return asyncio.run(self.handlers.logged_handler(name, handler)())
        finally:
            self.handlers.request = original

    def ok_of(self, response):
        self.assertEqual(response["_stub"], "json_response", "成功一律走 200")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def err_of(self, response):
        self.assertEqual(response["_stub"], "error_response", "转图失败必须是 500，不能糊成 200")
        self.assertEqual(response["status"], 500)
        return response


class TestCardImageRoute(CardImageHandlerCase):
    def test_route_is_registered_as_get(self) -> None:
        specs = {spec[0]: (spec[1], spec[2]) for spec in self.routes.ROUTES}
        self.assertEqual(specs["diary/card-image"], ("diary_card_image", ("GET",)))

    def test_handler_exists(self) -> None:
        self.assertIn("diary_card_image", self.handlers.build_handlers(self.deps))


class TestCardImageExport(CardImageHandlerCase):
    def test_returns_decodable_png(self) -> None:
        data = self.ok_of(self.call("diary_card_image", query={"book": "normal"}))
        self.assertTrue(data["ok"])
        self.assertFalse(data["empty"])
        self.assertEqual(data["mime"], "image/png")
        self.assertEqual(data["bytes"], len(PNG_BYTES))
        raw = base64.b64decode(data["data_url"].split(",", 1)[1])
        self.assertEqual(raw, PNG_BYTES, "回给浏览器的字节必须和出图的文件一模一样")

    def test_filename_carries_book_and_date(self) -> None:
        data = self.ok_of(self.call("diary_card_image", query={"book": "normal"}))
        self.assertEqual(data["date"], "2026-10-04")
        self.assertIn("2026-10-04", data["filename"])
        self.assertTrue(data["filename"].endswith(".png"))

    def test_uses_same_render_options_as_chat_card(self) -> None:
        """导出图和 /看日记 的图必须长得一样 → 同一份 RENDER_OPTIONS。"""
        from core.diary import card as card_mod

        self.call("diary_card_image", query={"book": "normal"})
        self.assertEqual(len(self.render_calls), 1)
        self.assertEqual(self.render_calls[0]["options"], card_mod.RENDER_OPTIONS)

    def test_passes_rendered_html_not_raw_template(self) -> None:
        """传进去的应该已经是渲好的 HTML（没有 {{ }} / {% %} 残留）。"""
        self.call("diary_card_image", query={"book": "normal"})
        html = self.render_calls[0]["template"]
        self.assertIn("2026-10-04", html)
        self.assertNotIn("{{", html)
        self.assertNotIn("{%", html)

    def test_empty_book_is_not_an_error(self) -> None:
        """这本没日记 → 200 + empty，前端据此提示，别当成导出失败。"""
        storage.atomic_write_text(self.layout.diary, "")
        data = self.ok_of(self.call("diary_card_image", query={"book": "normal"}))
        self.assertTrue(data["empty"])
        self.assertEqual(self.render_calls, [], "没内容就不该去出图")


class TestCardImageFailures(CardImageHandlerCase):
    def test_render_service_failure_is_500_with_reason(self) -> None:
        """远端挂 → PIL 会接住（保底）→ 三个全挂才 500。

        以前这条链路只有一条路，远端不通就是导出失败；第 20 步起出图有三条路，所以"导出失败"
        的前提变成了"三个引擎都不行"。
        """
        self.render_should_fail = True
        with _all_engines_dead():
            response = self.err_of(self.call("diary_card_image", query={"book": "normal"}))
        self.assertIn("导出失败", response["message"])

    def test_pil_can_serve_the_export_when_remote_is_down(self) -> None:
        """远端不通时 PIL 把图接住——面板导出也有底。"""
        from core.diary import card_pil

        ok, reason = card_pil.probe()
        if not ok:
            self.skipTest(f"这台机器不能 PIL 出图：{reason}")
        self.render_should_fail = True
        with _only_engine("pil"):
            data = self.ok_of(self.call("diary_card_image", query={"book": "normal"}))
        self.assertTrue(data["ok"])
        self.assertGreater(data["bytes"], 0)

    def test_missing_rendered_file_is_500(self) -> None:
        """出图"成功"但文件不在（宿主路径变了 / 被清理）——照样要报错，不是崩。"""
        self.rendered_file.unlink()
        with _all_engines_dead():
            self.err_of(self.call("diary_card_image", query={"book": "normal"}))


class TestStyleLabelsAreServed(CardImageHandlerCase):
    """面板的版式按钮直接用后端给的中文名，前端不再自己抄一份映射。"""

    def test_card_payload_carries_six_chinese_labels(self) -> None:
        data = self.ok_of(self.call("diary_card", query={"book": "normal"}))
        labels = data["style_labels"]
        self.assertEqual(
            labels,
            {
                "paper": "纸感",
                "ink": "墨信",
                "postcard": "明信片",
                "tape": "手帐",
                "dots": "点阵本",
                "seal": "火漆信笺",
            },
        )

    def test_labels_cover_every_offered_style(self) -> None:
        data = self.ok_of(self.call("diary_card", query={"book": "normal"}))
        self.assertEqual(set(data["styles"]), set(data["style_labels"]))


class TestPageNumberOnCard(CardImageHandlerCase):
    """页码印在卡片右下角（第 20 步）——日记本总有页码，缺了像一叠散纸。"""

    def render(self, style: str = "paper") -> str:
        return self.ok_of(self.call("diary_card", query={"book": "normal", "style": style}))["html"]

    def test_page_marker_is_printed(self) -> None:
        html = self.render()
        self.assertIn("第 1 / 1 页", html)

    def test_every_style_prints_it(self) -> None:
        for style in ("paper", "ink", "postcard", "tape", "dots", "seal"):
            with self.subTest(style=style):
                self.assertIn("第 1 / 1 页", self.render(style))

    def test_ink_keeps_its_centered_mark(self) -> None:
        """墨信那款页脚原本是居中一个 ✦；加了页码不能把它挤到左边。"""
        html = self.render("ink")
        self.assertIn("✦", html)
        self.assertIn('class="mark"', html)
        self.assertIn('class="page"', html)

    def test_older_templates_without_markers_still_render(self) -> None:
        """没给页码时模板走 ``{% if page_total %}`` 的空分支，右下角留空，不该崩。"""
        html = card_mod.render_card(
            "paper", card_mod.card_context(
                date="2026-10-04", weekday="周日", book_name="日记",
                entries=[{"time": "20:00", "mood": "", "who": "", "paragraphs": ["x"]}],
            )
        )
        self.assertNotIn("第 ", html)
        self.assertIn("她写下的这一天", html)


class TestPageNumberBounds(CardImageHandlerCase):
    """页码算错时宁可**不印**，也不能印出「第 9 / 3 页」。"""

    def ctx(self, index: int, total: int) -> dict:
        return card_mod.card_context(
            date="2026-10-04", weekday="周日", book_name="日记",
            entries=[{"time": "20:00", "mood": "", "who": "", "paragraphs": ["x"]}],
            page_index=index, page_total=total,
        )

    def test_normal_case_keeps_numbers(self) -> None:
        ctx = self.ctx(2, 5)
        self.assertEqual((ctx["page_index"], ctx["page_total"]), (2, 5))

    def test_zero_total_disables_marker(self) -> None:
        ctx = self.ctx(0, 0)
        self.assertEqual((ctx["page_index"], ctx["page_total"]), (0, 0))

    def test_out_of_range_is_cleared(self) -> None:
        ctx = self.ctx(9, 3)
        self.assertEqual((ctx["page_index"], ctx["page_total"]), (0, 0))

    def test_zero_total_with_nonzero_index_is_cleared(self) -> None:
        ctx = self.ctx(3, 0)
        self.assertEqual((ctx["page_index"], ctx["page_total"]), (0, 0))


if __name__ == "__main__":
    unittest.main()