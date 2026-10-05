"""立绘上传的数据面（第 5.3 步）：魔数判定、服务端文件名、index 原子写、data_url。

纯函数 + 一个 ``PortraitStore``，**零 astrbot import**（``core/`` 硬约束）——宿主的
上传对象以鸭子类型进来（只要求有 ``save``），读成字节后的一切判定都在这里。

**安全 stance（官方明写"不要信任 Page 传来的路径、文件名、格式或数值范围"）**：

- 文件名**服务端生成** ``p_<12 位 hex>.<ext>``，用户原始文件名只作展示来源且存
  **净化版**（去目录、去分隔符、限长 60）——路径穿越无从谈起；
- **格式按魔数判定**，不看 ``content_type``（客户端可伪造）；扩展名由魔数定；
- 大小 / 数量上限在这里拒，**一个字节不落盘**。

**落盘纪律（契约 §4）**：``index.json`` 走 ``storage.atomic_write_text`` + ``KeyedLocks``；
**先落图再写 index**，写 index 失败**回滚删图**（不留孤儿、不让 index 指空）；删除连
文件一起删；``snapshot`` 时"index 有、盘上没文件"的条目**跳过并顺手清 index**（自愈）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import json
import os
import re
import secrets
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import storage

PORTRAITS_DIR = "portraits"
"""数据目录下的立绘子目录（``Layout.portraits``，随 ``ensure()`` 创建）。"""

MAX_PORTRAIT_BYTES = 8 * 1024 * 1024
"""单张上限：8 MB（魔数判定之后按字节数拒）。"""

MAX_UPLOAD_BODY_BYTES = MAX_PORTRAIT_BYTES + 1024 * 1024
"""multipart 整体上限：8 MB 图 + 1 MB 外壳余量——``content_length`` 预检用它，
超大请求在框架层就被挡，不用先读进内存。"""

MAX_PORTRAITS = 20
"""总数上限：超了上传直接 400（否则 8 MB/张 会变成无上限的磁盘占用）。"""

MAX_NAME_LENGTH = 60
"""展示名净化后的长度上限。"""

ID_PATTERN = re.compile(r"^p_[0-9a-f]{12}\Z")
"""服务端生成的 id 形状（取数前先验它，路径穿越双保险）。

用 ``\\Z`` 而不是 ``$``：Python 的 ``$`` **允许结尾换行**，``"p_0123456789ab\\n"`` 也能过，
那会把一个带换行的文件名拼进路径。眼下取数路径只喂 index 里的 id（``snapshot`` /
``select`` / ``delete`` 都先与 index 比对），穿越不了——但那是"调用方碰巧安全"，
不是"这个正则安全"。"""

_MAGIC_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)
"""魔数 → (mime, 扩展名)。webp 的魔数带偏移，单独判（见 ``sniff_image``）。"""

_EXT_BY_MIME = {mime: ext for _magic, mime, ext in _MAGIC_SIGNATURES}
_EXT_BY_MIME["image/webp"] = ".webp"


class PortraitError(ValueError):
    """立绘校验 / 操作失败（调用方转 400）。"""


def sniff_image(data: bytes) -> tuple[str, str]:
    """按**魔数**判定图片格式，返回 ``(mime, 扩展名)``；认不出就抛 ``PortraitError``。

    ``content_type`` 一概不信（客户端可伪造）；webp 是 ``RIFF....WEBP``（偏移 8）。
    """
    for magic, mime, ext in _MAGIC_SIGNATURES:
        if data.startswith(magic):
            return mime, ext
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise PortraitError("只收 png / jpeg / webp / gif 图片（按文件内容判定，不看扩展名与 content_type）。")


def sanitize_name(raw: object, *, max_len: int = MAX_NAME_LENGTH) -> str:
    """展示名净化：去目录（两类分隔符都算）、去危险字符、限长；空了回落「未命名」。"""
    text = str(raw or "").replace("\\", "/")
    text = text.rsplit("/", 1)[-1]  # 只要文件名部分，目录信息全部丢掉
    text = "".join(ch for ch in text if ch.isprintable() and ch not in '\\"<>|:*?')
    text = text.strip().strip(".")  # 首尾点（Windows 保留 / 隐藏文件）
    return text[:max_len] or "未命名"


def new_portrait_id() -> str:
    """服务端生成 id：``p_`` + 12 位 hex（文件名同名不同扩展）。"""
    return "p_" + secrets.token_hex(6)


def data_url_of(mime: str, data: bytes) -> str:
    """``data:<mime>;base64,...``——前端直接塞 ``<img src>``。"""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


async def read_upload(upload: Any, tmp_dir: Path) -> bytes:
    """把宿主上传对象读成字节（save 的同步 / 异步双兼容，形照 meme_manager 的垫）。

    走「``save`` 到临时文件再读」而不是 ``upload.read()``：官方文档只承诺
    ``save()``，read 在部分宿主版本上不存在。``PluginUploadFile.save`` 在不同
    AstrBot 版本上同步 / 异步不一样——``inspect.iscoroutinefunction`` 为真就
    ``await``，否则 ``asyncio.to_thread``；直接 ``await`` 在同步版宿主上会 TypeError。
    """
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".part", dir=str(tmp_dir))
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        save = getattr(upload, "save", None)
        if not callable(save):
            raise PortraitError("这个宿主不支持文件上传（缺 save）。")
        if inspect.iscoroutinefunction(save):
            await save(str(tmp))
        else:
            result = await asyncio.to_thread(save, str(tmp))
            if inspect.isawaitable(result):
                await result
        return tmp.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写字节文件：同目录临时文件 → ``os.replace``（与 ``storage.atomic_write_text`` 同源）。"""
    path = Path(path)
    storage.ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class PortraitStore:
    """立绘的存储与索引。布局与锁由调用方注入（内核不认宿主）。"""

    def __init__(self, layout: storage.Layout, locks: storage.KeyedLocks) -> None:
        self.layout = layout
        self.locks = locks

    # ---- 路径 -------------------------------------------------------------------

    @property
    def base(self) -> Path:
        return self.layout.portraits

    @property
    def index_path(self) -> Path:
        return self.base / "index.json"

    def file_path(self, portrait_id: str, mime: str) -> Path | None:
        """id + mime → 磁盘文件；id 形状不对 / mime 不认识返回 ``None``（调用方按缺失处理）。"""
        if not ID_PATTERN.match(str(portrait_id or "")):
            return None
        ext = _EXT_BY_MIME.get(str(mime or ""))
        if ext is None:
            return None
        return self.base / f"{portrait_id}{ext}"  # id 已过白名单正则，无穿越可能

    # ---- index ------------------------------------------------------------------

    def read_index(self) -> dict[str, Any]:
        """读 index（坏 JSON / 形状不对一律降级为空表，不抛错）。"""
        doc = storage.read_json(self.index_path, default={})
        if not isinstance(doc, dict):
            return {"current": None, "items": []}
        items = [dict(item) for item in doc.get("items") or [] if isinstance(item, dict)]
        current = doc.get("current")
        if not isinstance(current, str):
            current = None
        return {"current": current, "items": items}

    def _write_index(self, doc: dict[str, Any]) -> None:
        storage.atomic_write_text(
            self.index_path,
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
        )
        storage.shared_cache.invalidate(self.index_path)

    @staticmethod
    def _meta(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "mime": str(item.get("mime") or ""),
            "bytes": int(item.get("bytes") or 0),
            "created_at": str(item.get("created_at") or ""),
        }

    def _item_with_data(self, item: Mapping[str, Any]) -> dict[str, Any] | None:
        """单条元数据 + ``data_url``；盘上没文件返回 ``None``（自愈由调用方清）。"""
        path = self.file_path(str(item.get("id") or ""), str(item.get("mime") or ""))
        if path is None or not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        result = self._meta(item)
        result["data_url"] = data_url_of(str(item.get("mime") or ""), data)
        return result

    # ---- 操作 -------------------------------------------------------------------

    async def upload(self, data: bytes, name: object, *, now: datetime) -> dict[str, Any]:
        """收一张图：校验（大小 / 魔数 / 数量）→ 先落图 → 再写 index（失败回滚删图）。

        ``now`` 由调用方传（适配器用 ``settings.zone()`` 现算）——core 禁裸 ``datetime.now()``。
        """
        if len(data) > MAX_PORTRAIT_BYTES:
            raise PortraitError("单张不能超过 8 MB。")
        mime, ext = sniff_image(data)
        async with self.locks.acquire(self.index_path):
            index = self.read_index()
            if len(index["items"]) >= MAX_PORTRAITS:
                raise PortraitError(f"最多 {MAX_PORTRAITS} 张，先删一张再传。")

            portrait_id = new_portrait_id()
            target = self.base / f"{portrait_id}{ext}"
            _atomic_write_bytes(target, data)  # 先落图
            item = {
                "id": portrait_id,
                "name": sanitize_name(name),
                "mime": mime,
                "bytes": len(data),
                "created_at": now.isoformat(timespec="seconds"),
            }
            try:
                index["items"] = [*index["items"], item]
                if not index["current"]:
                    index["current"] = portrait_id  # 第一张自动成为当前
                self._write_index(index)
            except OSError:
                with contextlib.suppress(OSError):
                    target.unlink(missing_ok=True)  # 回滚：不留孤儿，也不让 index 指空
                raise
        return {"ok": True, "id": portrait_id, "item": self._meta(item)}

    async def select(self, portrait_id: str) -> dict[str, Any]:
        """切换当前立绘；响应带新的 ``current``（含 data_url），前端不必再发一次请求。"""
        async with self.locks.acquire(self.index_path):
            index = self.read_index()
            item = next((entry for entry in index["items"] if entry.get("id") == portrait_id), None)
            if item is None:
                raise PortraitError("找不到这张立绘（可能已被删除），刷新后再试。")
            index["current"] = portrait_id
            self._write_index(index)
            current = self._item_with_data(item)
        return {"ok": True, "current": current}

    async def delete(self, portrait_id: str) -> dict[str, Any]:
        """删除一张（连文件）；删的是 current 就落到剩余第一张，全删完 ``current: None``。"""
        async with self.locks.acquire(self.index_path):
            index = self.read_index()
            remaining = [entry for entry in index["items"] if entry.get("id") != portrait_id]
            if len(remaining) == len(index["items"]):
                raise PortraitError("找不到这张立绘（可能已被删除），刷新后再试。")
            removed = next(entry for entry in index["items"] if entry.get("id") == portrait_id)
            path = self.file_path(str(removed.get("id") or ""), str(removed.get("mime") or ""))
            if path is not None and path.is_file():
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)  # 删除连文件一起删
            index["items"] = remaining
            if index["current"] == portrait_id:
                index["current"] = remaining[0]["id"] if remaining else None
            self._write_index(index)
            current = self._item_with_data(next(entry for entry in remaining if entry.get("id") == index["current"])) if index["current"] else None
        return {"ok": True, "current": current, "items": [self._meta(entry) for entry in remaining]}

    async def snapshot(self, *, only_id: str | None = None) -> dict[str, Any]:
        """``GET portrait`` 的数据面。

        不带 ``only_id``：``current``（含 data_url，无图 null）+ ``items``（只有元数据）；
        带 ``only_id``：单张（含 data_url），不存在抛 ``PortraitError``（400）。
        "index 有、盘上没文件"的条目跳过并顺手清 index（自愈）。
        """
        async with self.locks.acquire(self.index_path):
            index = self.read_index()
            alive = [entry for entry in index["items"] if self._exists(entry)]
            if len(alive) != len(index["items"]):
                # 自愈：盘上没了的条目清出 index（current 指向被清的就落到剩余第一张）
                index["items"] = alive
                if index["current"] and index["current"] not in {entry.get("id") for entry in alive}:
                    index["current"] = alive[0]["id"] if alive else None
                self._write_index(index)
            if only_id is not None:
                item = next((entry for entry in alive if entry.get("id") == only_id), None)
                if item is None:
                    raise PortraitError("找不到这张立绘（可能已被删除），刷新后再试。")
                with_data = self._item_with_data(item)
                return {"ok": True, "item": with_data}
            current = next((entry for entry in alive if entry.get("id") == index["current"]), None)
            return {
                "ok": True,
                "current": self._item_with_data(current) if current else None,
                "items": [self._meta(entry) for entry in alive],
            }

    # ---- 内部 -------------------------------------------------------------------

    def _exists(self, item: Mapping[str, Any]) -> bool:
        path = self.file_path(str(item.get("id") or ""), str(item.get("mime") or ""))
        return path is not None and path.is_file()


__all__ = [
    "ID_PATTERN",
    "MAX_NAME_LENGTH",
    "MAX_PORTRAITS",
    "MAX_UPLOAD_BODY_BYTES",
    "MAX_PORTRAIT_BYTES",
    "PORTRAITS_DIR",
    "PortraitError",
    "PortraitStore",
    "data_url_of",
    "new_portrait_id",
    "read_upload",
    "sanitize_name",
    "sniff_image",
]
