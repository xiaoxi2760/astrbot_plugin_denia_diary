"""``web_api`` 三条生图路由的 handler 层测试。

桩照 ``test_web_api_card_image.py`` 那一套：同一个 ``web_api._web`` 离线桩，
真 Imagery 门面 + 桩 transport。钉住三件事：

* 成功 / 业务失败都走 **200 + ``{ok, ...}``**（生图失败是业务结果，不是请求形状错误）；
* body 不是 JSON 对象 → 400；
* handler 不做任何业务判断——闸门在 core，这里只接线。
"""

from __future__ import annotations

import asyncio
import base64
import json
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
from core.imagery.api import Imagery  # noqa: E402
from core.imagery.providers import HttpResponse  # noqa: E402
from core.life import Life  # noqa: E402
from core.state import State  # noqa: E402
from core.state.store import StateStore  # noqa: E402
from core.webui_portrait import PortraitStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 26, 10, 30, tzinfo=TZ)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEklEQVR4nGP8sEWDgYGBiQEMABaCAdDy4LuvAAAAAElFTkSuQmCC")
PNG_B64 = base64.b64encode(PNG).decode("ascii")


def _async_value(value):
    async def getter(*args, **kwargs):
        return value

    return getter()


class ImageryHandlerCase(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers, routes

        self.handlers = handlers
        self.routes = routes
        self.requests: list = []
        self.settings = settings_mod.load_settings(
            {
                "timezone": "Asia/Shanghai",
                "imagery": {"enabled": True, "provider": "a1111", "base_url": "http://127.0.0.1:7860", "daily_limit": 3},
            }
        )
        self.layout = storage.Layout(self.root).ensure()
        self.locks = storage.KeyedLocks()
        self.deps = SimpleNamespace(
            settings=self.settings,
            layout=self.layout,
            life=Life(settings=self.settings, layout=self.layout, locks=self.locks),
            state=State(
                settings=self.settings,
                layout=self.layout,
                store=StateStore(layout=self.layout, locks=self.locks),
                locks=self.locks,
            ),
            portraits=PortraitStore(self.layout, self.locks),
            imagery=None,
            _now=lambda: NOW,
        )

        def transport(req):
            self.requests.append(req)
            return HttpResponse(200, json.dumps({"images": [PNG_B64], "info": "{}"}).encode("utf-8"))

        self.deps.imagery = Imagery(
            settings=self.settings,
            layout=self.layout,
            locks=self.locks,
            portraits=self.deps.portraits,
            life=self.deps.life,
            state=self.deps.state,
            transport=transport,
        )

    def call(self, name: str, *, body: object = None, query=None):
        fake = SimpleNamespace(
            query=query or {},
            username="admin",
            json=lambda default=None: _async_value(body),
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
        self.assertEqual(response["_stub"], "error_response")
        return response


class RouteTableTest(ImageryHandlerCase):
    def test_routes_registered(self) -> None:
        specs = {spec[0]: (spec[1], spec[2]) for spec in self.routes.ROUTES}
        self.assertEqual(specs["imagery"], ("imagery_status", ("GET",)))
        self.assertEqual(specs["imagery/image"], ("imagery_image", ("GET",)))
        self.assertEqual(specs["imagery/generate"], ("imagery_generate", ("POST",)))

    def test_handlers_exist(self) -> None:
        names = self.handlers.build_handlers(self.deps)
        for name in ("imagery_status", "imagery_image", "imagery_generate"):
            self.assertIn(name, names)


class ImageryStatusHandlerTest(ImageryHandlerCase):
    def test_status_ok_200(self) -> None:
        data = self.ok_of(self.call("imagery_status"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertEqual(data["daily"]["limit"], 3)

    def test_status_read_only(self) -> None:
        self.ok_of(self.call("imagery_status"))
        self.assertEqual(self.requests, [], "状态页不发任何模型请求")


class ImageryImageHandlerTest(ImageryHandlerCase):
    def test_no_image_yet_is_200_ok_false(self) -> None:
        data = self.ok_of(self.call("imagery_image"))
        self.assertFalse(data["ok"])
        self.assertIn("error", data)

    def test_returns_decodable_image(self) -> None:
        asyncio.run(self.deps.imagery.generate(now=NOW, trusted=True))
        data = self.ok_of(self.call("imagery_image"))
        self.assertTrue(data["ok"])
        raw = base64.b64decode(data["image"]["data_url"].split(",", 1)[1])
        self.assertEqual(raw, PNG)


class ImageryGenerateHandlerTest(ImageryHandlerCase):
    def test_generate_success(self) -> None:
        data = self.ok_of(self.call("imagery_generate", body={}))
        self.assertTrue(data["ok"])
        self.assertTrue(data["image"]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(data["daily"]["used"], 1)

    def test_generate_disabled_is_200_ok_false(self) -> None:
        """功能关着是业务结果：200 + ok:false，前端据此展示，不当成服务端故障。"""
        self.deps.settings = settings_mod.load_settings(
            {"timezone": "Asia/Shanghai", "imagery": {"enabled": False}}
        )
        self.deps.imagery.settings = self.deps.settings
        data = self.ok_of(self.call("imagery_generate", body={}))
        self.assertFalse(data["ok"])
        self.assertIn("关闭", data["error"])
        self.assertEqual(self.requests, [])

    def test_generate_non_object_body_is_400(self) -> None:
        response = self.call("imagery_generate", body="not-a-dict")
        self.err_of(response)
        self.assertEqual(response["status"], 400)

    def test_generate_uses_trusted_path(self) -> None:
        """面板 handler 必须 trusted=True——面板是主人视角（与 life 写操作同一口径）。"""
        captured: dict = {}

        async def spy(*args, **kwargs):
            captured.update(kwargs)
            return {"ok": False, "error": "stub"}

        original = self.deps.imagery.generate
        self.deps.imagery.generate = spy
        try:
            self.ok_of(self.call("imagery_generate", body={}))
        finally:
            self.deps.imagery.generate = original
        self.assertTrue(captured.get("trusted"), "面板触发生图必须走 trusted 通道")


if __name__ == "__main__":
    unittest.main()
