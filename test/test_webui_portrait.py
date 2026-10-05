"""立绘上传测试（第 5.3 步）：魔数判定 / 大小与数量上限 / 路径穿越 / 落盘纪律 / 自愈。

`PortraitStore` 的用例直接跑真文件（``TmpDirCase`` 临时目录）；handler 层用假
``request``（照 ``test_webui_settings.py`` 的做法），save 的同步 / 异步双兼容
各验一遍——官方明说不同 AstrBot 版本上它不一样，直接 ``await`` 会在同步版宿主上
TypeError（契约 §四 03:20 裁定 #6）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import TmpDirCase  # noqa: E402

from core import storage  # noqa: E402
from core import webui_portrait  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 6, 3, 30, tzinfo=TZ)
UMO = "aiocqhttp:FriendMessage:10001"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff" + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 32
NOT_AN_IMAGE = b"just some text pretending to be a picture"


def run(coro):
    return asyncio.run(coro)


def make_store(root: Path) -> webui_portrait.PortraitStore:
    layout = storage.Layout(root).ensure()
    return webui_portrait.PortraitStore(layout=layout, locks=storage.KeyedLocks())


class FakeUpload:
    """``PluginUploadFile`` 替身：save 可配同步 / 异步 / 坏掉。"""

    def __init__(self, data: bytes, filename: str = "图.png", *, async_save: bool = False) -> None:
        self.data = data
        self.filename = filename
        self.async_save = async_save
        self.saved_to: list[str] = []

    def save(self, path: str) -> None:
        self.saved_to.append(str(path))
        if self.async_save:  # 某些宿主的 save 其实返回协程（不是 coroutine function）
            return self._async_write(path)
        Path(path).write_bytes(self.data)

    async def _async_write(self, path: str) -> None:
        Path(path).write_bytes(self.data)


class SniffTest(unittest.TestCase):
    def test_four_formats_by_magic(self) -> None:
        for data, mime, ext in (
            (PNG_BYTES, "image/png", ".png"),
            (JPEG_BYTES, "image/jpeg", ".jpg"),
            (GIF_BYTES, "image/gif", ".gif"),
            (WEBP_BYTES, "image/webp", ".webp"),
        ):
            got_mime, got_ext = webui_portrait.sniff_image(data)
            self.assertEqual((got_mime, got_ext), (mime, ext))

    def test_content_type_is_never_trusted(self) -> None:
        """魔数不对就拒——就算前端声称是 png（客户端可伪造）。"""
        with self.assertRaises(webui_portrait.PortraitError):
            webui_portrait.sniff_image(NOT_AN_IMAGE)


class SanitizeNameTest(unittest.TestCase):
    def test_strips_path_traversal(self) -> None:
        for raw in ("../../etc/passwd", "..\\..\\windows\\evil.png", "/abs/path/x.png"):
            cleaned = webui_portrait.sanitize_name(raw)
            self.assertNotIn("/", cleaned)
            self.assertNotIn("\\", cleaned)
            self.assertNotIn("..", cleaned)

    def test_length_limit_and_fallback(self) -> None:
        self.assertEqual(len(webui_portrait.sanitize_name("长" * 200)), 60)
        self.assertEqual(webui_portrait.sanitize_name(""), "未命名")
        self.assertEqual(webui_portrait.sanitize_name("///"), "未命名")

    def test_id_pattern_rejects_traversal(self) -> None:
        """id 白名单正则是路径穿越的第二道闸（文件路径只从 id+mime 拼出来）。"""
        store = make_store(Path("."))  # 不真落盘，只测 file_path
        self.assertIsNone(store.file_path("../evil", "image/png"))
        self.assertIsNone(store.file_path("p_x", "image/png"))
        self.assertIsNone(store.file_path("p_0123456789ab", "image/unknown"))
        self.assertIsNotNone(store.file_path("p_0123456789ab", "image/png"))


class StoreTest(TmpDirCase):
    def store(self) -> webui_portrait.PortraitStore:
        return make_store(self.root)

    def test_upload_then_snapshot(self) -> None:
        store = self.store()
        result = run(store.upload(PNG_BYTES, "樱花.png", now=NOW))
        self.assertTrue(result["ok"])
        self.assertRegex(result["id"], webui_portrait.ID_PATTERN)
        self.assertEqual(result["item"]["name"], "樱花.png")
        # 文件名服务端生成：p_<12hex>.png，与用户原始文件名无关
        files = [p.name for p in store.base.iterdir() if p.name != "index.json"]
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^p_[0-9a-f]{12}\.png$")

        snap = run(store.snapshot())
        self.assertEqual(snap["items"][0]["bytes"], len(PNG_BYTES))
        self.assertTrue(snap["current"]["data_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("data_url", snap["items"][0], "items 只有元数据，不带 data_url")

    def test_first_upload_becomes_current(self) -> None:
        store = self.store()
        first = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        second = run(store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        snap = run(store.snapshot())
        self.assertEqual(snap["current"]["id"], first["id"], "第一张自动成为当前")
        self.assertEqual(len(snap["items"]), 2)
        self.assertNotEqual(snap["current"]["id"], second["id"])

    def test_too_large_rejected_without_writing(self) -> None:
        store = self.store()
        big = PNG_BYTES + b"\x00" * (webui_portrait.MAX_PORTRAIT_BYTES)
        with self.assertRaises(webui_portrait.PortraitError):
            run(store.upload(big, "big.png", now=NOW))
        self.assertEqual(list(store.base.glob("p_*")), [], "一个字节都不落盘")

    def test_too_many_rejected(self) -> None:
        store = self.store()
        for _ in range(webui_portrait.MAX_PORTRAITS):
            run(store.upload(PNG_BYTES, "a.png", now=NOW))
        with self.assertRaises(webui_portrait.PortraitError) as caught:
            run(store.upload(PNG_BYTES, "a.png", now=NOW))
        self.assertIn("20", str(caught.exception))
        self.assertEqual(len(run(store.snapshot())["items"]), webui_portrait.MAX_PORTRAITS)

    def test_index_write_failure_rolls_back_the_file(self) -> None:
        """先落图再写 index；写 index 失败 → 回滚删图，不留孤儿也不让 index 指空。"""
        store = self.store()
        with mock.patch.object(storage, "atomic_write_text", side_effect=OSError("磁盘满了")):
            with self.assertRaises(OSError):
                run(store.upload(PNG_BYTES, "a.png", now=NOW))
        self.assertEqual(list(store.base.glob("p_*")), [], "刚落的图必须被回滚删掉")
        self.assertEqual(run(store.snapshot())["items"], [])

    def test_select_returns_current_with_data_url(self) -> None:
        store = self.store()
        first = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        second = run(store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        result = run(store.select(first["id"]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["current"]["id"], first["id"])
        self.assertTrue(result["current"]["data_url"].startswith("data:image/png;base64,"))
        snap = run(store.snapshot())
        self.assertEqual(snap["current"]["id"], first["id"])
        self.assertEqual([item["id"] for item in snap["items"]], [first["id"], second["id"]],
                         "items 保持插入序")

    def test_select_unknown_id_is_rejected(self) -> None:
        store = self.store()
        with self.assertRaises(webui_portrait.PortraitError):
            run(store.select("p_000000000000"))

    def test_delete_removes_file_and_falls_back_current(self) -> None:
        store = self.store()
        first = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        second = run(store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        first_file = next(p for p in store.base.glob("p_*") if p.suffix == ".png")
        result = run(store.delete(first["id"]))
        self.assertFalse(first_file.exists(), "删除连文件一起删")
        self.assertEqual(result["current"]["id"], second["id"], "删 current 落到剩余第一张")
        self.assertEqual([item["id"] for item in result["items"]], [second["id"]])

    def test_delete_last_one_clears_current(self) -> None:
        store = self.store()
        only = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        result = run(store.delete(only["id"]))
        self.assertIsNone(result["current"])
        self.assertEqual(result["items"], [])
        self.assertEqual(run(store.snapshot())["current"], None)

    def test_delete_unknown_id_is_rejected(self) -> None:
        store = self.store()
        with self.assertRaises(webui_portrait.PortraitError):
            run(store.delete("p_000000000000"))

    def test_self_healing_skips_missing_files_and_cleans_index(self) -> None:
        """index 有、盘上没文件 → 条目跳过并顺手清 index；current 被清就落到剩余第一张。"""
        store = self.store()
        first = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        second = run(store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        run(store.select(first["id"]))
        # 手动删掉第一张的文件（模拟盘上丢失）
        for p in store.base.glob("p_*.png"):
            p.unlink()
        snap = run(store.snapshot())
        self.assertEqual([item["id"] for item in snap["items"]], [second["id"]])
        self.assertEqual(snap["current"]["id"], second["id"], "current 指向的图没了 → 落到剩余第一张")
        persisted = json.loads(store.index_path.read_text(encoding="utf-8"))
        self.assertEqual([item["id"] for item in persisted["items"]], [second["id"]], "index 真的被清了")

    def test_get_single_by_id(self) -> None:
        store = self.store()
        first = run(store.upload(PNG_BYTES, "a.png", now=NOW))
        run(store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        result = run(store.snapshot(only_id=first["id"]))
        self.assertTrue(result["ok"])
        self.assertEqual(result["item"]["id"], first["id"])
        self.assertTrue(result["item"]["data_url"].startswith("data:image/png;base64,"))
        with self.assertRaises(webui_portrait.PortraitError):
            run(store.snapshot(only_id="p_000000000000"))

    def test_empty_gallery_is_200_not_error(self) -> None:
        store = self.store()
        snap = run(store.snapshot())
        self.assertEqual(snap, {"ok": True, "current": None, "items": []})

    def test_read_upload_supports_sync_and_async_save(self) -> None:
        store = self.store()
        sync_upload = FakeUpload(PNG_BYTES, "同步.png")
        async_style = FakeUpload(JPEG_BYTES, "异步.jpg", async_save=True)
        self.assertEqual(run(webui_portrait.read_upload(sync_upload, store.base)), PNG_BYTES)
        self.assertEqual(run(webui_portrait.read_upload(async_style, store.base)), JPEG_BYTES)
        self.assertEqual(list(store.base.glob(".upload-*")), [], "临时文件用完即清")

    def test_read_upload_without_save_is_rejected(self) -> None:
        store = self.store()
        with self.assertRaises(webui_portrait.PortraitError):
            run(webui_portrait.read_upload(SimpleNamespace(), store.base))


class TestPortraitHandlers(TmpDirCase):
    """handler 薄壳（假 request）：content_length 预检 / files() 取 file 字段 / 400 语义。"""

    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers

        self.handlers = handlers
        self.store = make_store(self.root)
        self.deps = SimpleNamespace(portraits=self.store, _now=lambda: NOW)

    def call(self, name: str, *, body=None, files=None, content_length=None, query=None):
        async def read_files():
            return files

        fake = SimpleNamespace(
            query=query or {},
            username="admin",
            content_length=content_length,
            json=lambda default=None: _async_value(body if body is not None else default),
            files=read_files,
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)[name]
            return asyncio.run(self.handlers.logged_handler(name, handler)())
        finally:
            self.handlers.request = original

    def data_of(self, response):
        self.assertEqual(response["_stub"], "json_response")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def test_get_empty_is_200(self) -> None:
        data = self.data_of(self.call("portrait_get"))
        self.assertEqual(data["current"], None)
        self.assertEqual(data["items"], [])

    def test_get_with_id_query(self) -> None:
        first = run(self.store.upload(PNG_BYTES, "a.png", now=NOW))
        data = self.data_of(self.call("portrait_get", query={"id": first["id"]}))
        self.assertEqual(data["item"]["id"], first["id"])

    def test_upload_happy_path(self) -> None:
        response = self.call(
            "portrait_upload",
            files={"file": FakeUpload(PNG_BYTES, "../../樱花.png")},
        )
        data = self.data_of(response)
        self.assertTrue(data["ok"])
        self.assertRegex(data["id"], webui_portrait.ID_PATTERN)
        self.assertEqual(data["item"]["name"], "樱花.png", "展示名净化（去目录）")

    def test_upload_pretending_content_type_still_checked(self) -> None:
        response = self.call(
            "portrait_upload",
            files={"file": FakeUpload(NOT_AN_IMAGE, "假图.png")},
        )
        self.assertEqual(response["status"], 400)
        self.assertEqual(list(self.store.base.glob("p_*")), [], "不合规一个字节都不落盘")

    def test_upload_content_length_precheck_rejects_early(self) -> None:
        """超大 body 在读文件之前就 400（两层保险之一）。"""
        called = {"files": False}

        async def files():
            called["files"] = True
            return {}

        fake = SimpleNamespace(
            query={},
            username="admin",
            content_length=webui_portrait.MAX_UPLOAD_BODY_BYTES + 1,
            files=files,
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)["portrait_upload"]
            response = asyncio.run(self.handlers.logged_handler("portrait_upload", handler)())
        finally:
            self.handlers.request = original
        self.assertEqual(response["status"], 400)
        self.assertFalse(called["files"], "预检拦截后不该再去读 multipart")

    def test_upload_without_file_field_is_400(self) -> None:
        response = self.call("portrait_upload", files={})
        self.assertEqual(response["status"], 400)
        self.assertIn("file", response["message"])

    def test_select_and_delete_need_id(self) -> None:
        for name in ("portrait_select", "portrait_delete"):
            for body in (None, {}, {"id": ""}):
                response = self.call(name, body=body)
                self.assertEqual(response["status"], 400, f"{name} body={body!r}")

    def test_select_and_delete_unknown_id_is_400(self) -> None:
        for name in ("portrait_select", "portrait_delete"):
            response = self.call(name, body={"id": "p_000000000000"})
            self.assertEqual(response["status"], 400, name)

    def test_select_and_delete_roundtrip(self) -> None:
        first = run(self.store.upload(PNG_BYTES, "a.png", now=NOW))
        second = run(self.store.upload(JPEG_BYTES, "b.jpg", now=NOW))
        data = self.data_of(self.call("portrait_select", body={"id": first["id"]}))
        self.assertEqual(data["current"]["id"], first["id"])
        data = self.data_of(self.call("portrait_delete", body={"id": second["id"]}))
        self.assertEqual(data["current"]["id"], first["id"])
        self.assertEqual(len(data["items"]), 1)

    def test_route_table_has_sixteen_routes(self) -> None:
        from web_api import routes

        portrait_routes = [spec for spec in routes.ROUTES if spec[0].startswith("portrait")]
        self.assertEqual(
            [spec[0] for spec in portrait_routes],
            ["portrait", "portrait/upload", "portrait/select", "portrait/delete"],
        )
        self.assertEqual([spec[2] for spec in portrait_routes], [("GET",), ("POST",), ("POST",), ("POST",)])
        self.assertEqual(len(routes.ROUTES), 16, "12 条既有 + 立绘 4 条")


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
