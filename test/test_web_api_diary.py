"""日记改 / 删 / 回收站这 4 条路由的 **handler 层**测试（第 15 步）。

为什么单开一个文件：项目里"错误约定"是写死的硬规则——**业务结果一律 200 +
``{ok, error}``，只有请求形状不对才 400**。这条规则光在 core 层测不出来：handler
把"太旧"错当成 400、把"缺 seg_id"错当成 200，离线全绿，真机 iframe 里只透得出一个
字符串，前端根本分不清"坏了"和"你选的太旧"。

所以这里把两条路都钉住：

* ``{"ok": false, error}`` + HTTP 200 —— 找不到、太旧、已经删过、已经还原过；
* ``error_response`` + 400 —— body 不是对象、缺 ``seg_id`` / ``id`` / ``text``。

桩照 ``test_webui_settings.py`` 的 ``TestSettingsHandlers`` 那一套（同一个
``web_api._web`` 离线桩，不另造一个宽容的）。
"""

from __future__ import annotations

import asyncio
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
from core.diary import format as fmt  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=TZ)

SEED = (
    "2026-06-01 09:00（开心）〔u_1001〕\n很旧的一条。\n\n"
    "2026-10-04 20:00（难过）\n新的一条。\n\n"
)


async def _async_value(value):
    return value


class DiaryWriteHandlerCase(TmpDirCase):
    """一份真的 store + 一个只认必要属性的 ``deps``。"""

    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers, routes

        self.handlers = handlers
        self.routes = routes
        self.layout = storage.Layout(self.root).ensure()
        self.diary_store = DiaryStore(layout=self.layout, locks=storage.KeyedLocks())
        storage.atomic_write_text(self.layout.diary, SEED)
        self.deps = SimpleNamespace(
            settings=settings_mod.load_settings({"timezone": "Asia/Shanghai"}),
            layout=self.layout,
            diary=SimpleNamespace(store=self.diary_store),
            _now=lambda: NOW,
        )

    def call(self, name: str, *, body=None, query=None):
        fake = SimpleNamespace(
            query=query or {},
            username="admin",
            json=lambda default=None: _async_value(body if body is not None else default),
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)[name]
            return asyncio.run(self.handlers.logged_handler(name, handler)())
        finally:
            self.handlers.request = original

    def ids(self):
        payload = self.diary_store.read(fmt.NORMAL)
        return [fmt.seg_id(seg) for seg in fmt.split_segments(payload)]

    def ok_of(self, response):
        self.assertEqual(response["_stub"], "json_response", "业务结果必须走 200")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def bad_of(self, response):
        self.assertEqual(response["_stub"], "error_response", "形状不对才 400")
        self.assertEqual(response["status"], 400)
        return response  # message 在信封顶层，endpoint 之类在 data 里


class TestDiaryRouteTable(DiaryWriteHandlerCase):
    def test_four_diary_write_routes_are_registered(self) -> None:
        specs = {spec[0]: (spec[1], spec[2]) for spec in self.routes.ROUTES}
        self.assertEqual(
            specs["diary/rewrite"], ("diary_rewrite", ("POST",))
        )
        self.assertEqual(specs["diary/delete"], ("diary_delete", ("POST",)))
        self.assertEqual(specs["diary/trash"], ("diary_trash", ("GET",)))
        self.assertEqual(specs["diary/restore"], ("diary_restore", ("POST",)))

    def test_every_diary_handler_exists_in_build_handlers(self) -> None:
        built = self.handlers.build_handlers(self.deps)
        for name in ("diary_rewrite", "diary_delete", "diary_trash", "diary_restore"):
            self.assertIn(name, built, name)


class TestDiaryRewriteRoute(DiaryWriteHandlerCase):
    def test_happy_path(self) -> None:
        data = self.ok_of(self.call("diary_rewrite", body={
            "book": fmt.NORMAL, "seg_id": self.ids()[1], "text": "改过的。",
        }))
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "rewrite")
        self.assertEqual(data["before"], "新的一条。")
        texts = [e.text for e in fmt.parse_entries(self.diary_store.read(fmt.NORMAL))]
        self.assertEqual(texts, ["很旧的一条。", "改过的。"])

    def test_non_object_body_is_400(self) -> None:
        """body 是数组 / 字符串（不是对象）也走 400，别当空 body 一路走下去。"""
        response = self.bad_of(self.call("diary_rewrite", body=["不是对象"]))
        self.assertIn("JSON", response["message"])
        self.assertEqual(response["data"]["endpoint"], "diary/rewrite")

    def test_missing_seg_id_is_400(self) -> None:
        response = self.bad_of(self.call("diary_rewrite", body={"book": fmt.NORMAL, "text": "x"}))
        self.assertIn("seg_id", response["message"])

    def test_blank_text_is_400(self) -> None:
        """空正文是**形状**不对（连要写什么都不知道），不是业务结果。"""
        response = self.bad_of(self.call("diary_rewrite", body={
            "book": fmt.NORMAL, "seg_id": self.ids()[1], "text": "   ",
        }))
        self.assertIn("text", response["message"])

    def test_too_old_is_a_business_error_not_400(self) -> None:
        data = self.ok_of(self.call("diary_rewrite", body={
            "book": fmt.NORMAL, "seg_id": self.ids()[0], "text": "x",
        }))
        self.assertFalse(data["ok"])
        self.assertIn("太旧", data["error"])

    def test_unknown_seg_id_is_a_business_error(self) -> None:
        data = self.ok_of(self.call("diary_rewrite", body={
            "book": fmt.NORMAL, "seg_id": "0123456789ab", "text": "x",
        }))
        self.assertFalse(data["ok"])
        self.assertIn("找不到", data["error"])


class TestDiaryDeleteRoute(DiaryWriteHandlerCase):
    def test_happy_path_archives_it(self) -> None:
        data = self.ok_of(self.call("diary_delete", body={
            "book": fmt.NORMAL, "seg_id": self.ids()[1],
        }))
        self.assertTrue(data["ok"])
        self.assertTrue(data["trashed"])
        self.assertEqual(len(self.diary_store.trash_items()), 1)

    def test_missing_seg_id_is_400(self) -> None:
        self.bad_of(self.call("diary_delete", body={"book": fmt.NORMAL}))

    def test_already_deleted_is_a_business_error(self) -> None:
        seg_id = self.ids()[1]
        self.call("diary_delete", body={"book": fmt.NORMAL, "seg_id": seg_id})
        again = self.ok_of(self.call("diary_delete", body={"book": fmt.NORMAL, "seg_id": seg_id}))
        self.assertFalse(again["ok"])
        self.assertIn("找不到", again["error"])


class TestDiaryTrashAndRestoreRoutes(DiaryWriteHandlerCase):
    def _delete_one(self) -> str:
        self.call("diary_delete", body={"book": fmt.NORMAL, "seg_id": self.ids()[1]})
        return self.ok_of(self.call("diary_trash"))["items"][0]["id"]

    def test_trash_lists_what_was_deleted(self) -> None:
        trash_id = self._delete_one()
        data = self.ok_of(self.call("diary_trash"))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["items"][0]["id"], trash_id)
        self.assertEqual(data["items"][0]["preview"], "新的一条。")
        self.assertEqual(data["cap"], 200)

    def test_trash_ignores_query_args(self) -> None:
        self.ok_of(self.call("diary_trash", query={"book": fmt.LOVE}))

    def test_restore_round_trip(self) -> None:
        trash_id = self._delete_one()
        data = self.ok_of(self.call("diary_restore", body={"id": trash_id}))
        self.assertTrue(data["ok"])
        self.assertEqual(self.diary_store.read(fmt.NORMAL), SEED)  # 逐字回来
        self.assertEqual(self.ok_of(self.call("diary_trash"))["count"], 0)

    def test_restore_missing_id_is_400(self) -> None:
        self.bad_of(self.call("diary_restore", body={}))

    def test_restore_unknown_id_is_a_business_error(self) -> None:
        data = self.ok_of(self.call("diary_restore", body={"id": "seg-没有这条"}))
        self.assertFalse(data["ok"])
        self.assertIn("找不到", data["error"])

    def test_restore_twice_is_a_business_error(self) -> None:
        trash_id = self._delete_one()
        self.ok_of(self.call("diary_restore", body={"id": trash_id}))
        again = self.ok_of(self.call("diary_restore", body={"id": trash_id}))
        self.assertFalse(again["ok"])


class TestDiaryContentRoute(DiaryWriteHandlerCase):
    """``diary/content`` 现在会带 ``seg_id`` / ``editable``——前端靠它画按钮。"""

    def test_content_carries_the_write_handles(self) -> None:
        data = self.ok_of(self.call("diary_content", query={"book": fmt.NORMAL, "tail": "20"}))
        self.assertTrue(data["edit_window"]["can_edit"])
        self.assertEqual(data["edit_window"]["within_days"], 7)
        self.assertEqual([e["editable"] for e in data["entries"]], [False, True])
        self.assertTrue(all(e["seg_id"] for e in data["entries"]))

    def test_seg_id_from_the_route_is_the_one_the_write_route_wants(self) -> None:
        """端到端：读回的 id 直接拿去改，必须改得动（拼错了就是 404 人生）。"""
        data = self.ok_of(self.call("diary_content", query={"book": fmt.NORMAL}))
        seg_id = [e for e in data["entries"] if e["editable"]][0]["seg_id"]
        written = self.ok_of(self.call("diary_rewrite", body={
            "book": fmt.NORMAL, "seg_id": seg_id, "text": "改过了。",
        }))
        self.assertTrue(written["ok"])


if __name__ == "__main__":
    unittest.main()
