"""生图结果缓存（任务 2 第 4 条）：键寻址、原子落盘、独立目录。

- 缓存目录是数据目录下的 ``image-cache/``（``Layout.image_cache``），**与日记卡缓存
  （``card-cache/``）分开**——两类派生数据的失效逻辑完全不同，混在一起会让
  "删缓存"的语义说不清；
- 缓存键由调用方（``api.py``）把全部影响画面的要素（提示词、模型、参数、立绘指纹）
   合成一个 canonical JSON，这里只管 ``sha256``；
- 落盘纪律与 ``storage`` 同源：同目录临时文件 → ``os.replace``，失败不留半截文件；
- 每张图带一个同名 ``.json`` sidecar（提示词、模型、时间），面板要展示"上一张依据
  什么画的"；
- ``cleanup_tmp`` 在插件启动时调用：上次进程死在生成中途的 ``.part`` 残骸一律清掉
  （任务 2 第 5 条"重启能正确收尾"）。

只依赖标准库；不 import astrbot。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import storage

TMP_SUFFIX = ".part"
"""生成中 / 传输中的临时文件后缀：启动清理只删它，不碰正式图。"""

MAX_IMAGE_BYTES = 20 * 1024 * 1024
"""单张缓存上限 20 MB：适配层返回的东西再大就是异常（超限在存储前拒）。"""

MAX_CACHE_IMAGES = 40
"""最多保留 40 张已生成图片。"""

MAX_CACHE_BYTES = 200 * 1024 * 1024
"""图片与 sidecar 合计最多占用 200 MiB。"""

_META_SUFFIX = ".json"
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNED_FILE_RE = re.compile(r"^([0-9a-f]{32})(\.png|\.jpg|\.jpeg|\.webp|\.gif|\.json)$")
_TMP_RE = re.compile(r"^\.([0-9a-f]{32}(?:\.png|\.jpg|\.jpeg|\.webp|\.gif|\.json))\.part[A-Za-z0-9_-]+$")


def _validate_key(key: str) -> str:
    text = str(key)
    if not _KEY_RE.fullmatch(text):
        raise ValueError("缓存键必须是 32 位小写十六进制字符串")
    return text


def key_for(payload: Mapping[str, Any]) -> str:
    """缓存要素 → 32 位 hex 键。``payload`` 必须是 JSON 可序列化的纯数据。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


class ImageCache:
    """``image-cache/`` 目录的读写。布局由调用方注入（内核不认宿主）。"""

    def __init__(self, base: Path) -> None:
        self.base = Path(base)

    # ---- 路径 -------------------------------------------------------------------

    def image_path(self, key: str, ext: str) -> Path:
        safe_key = _validate_key(key)
        safe_ext = str(ext).lower()
        if safe_ext not in _IMAGE_EXTENSIONS:
            raise ValueError("缓存图片扩展名无效")
        return self.base / f"{safe_key}{safe_ext}"

    def meta_path(self, key: str) -> Path:
        return self.base / f"{_validate_key(key)}{_META_SUFFIX}"

    # ---- 存取 -------------------------------------------------------------------

    def find(self, key: str) -> Path | None:
        """键 → 已缓存的图；没有 / 元数据缺失（半截写入）返回 ``None``。"""
        _validate_key(key)
        for ext in _IMAGE_EXTENSIONS:
            path = self.image_path(key, ext)
            if path.is_file() and self.meta_path(key).is_file():
                return path
        return None

    def read_meta(self, key: str) -> dict[str, Any] | None:
        doc = storage.read_json(self.meta_path(key), default=None)
        return doc if isinstance(doc, dict) else None

    def store(self, key: str, data: bytes, ext: str, meta: dict[str, Any]) -> Path:
        """先落图、再落 sidecar（半截状态 = 图在 meta 不在，``find`` 视为未命中）。

        任何一步失败抛 ``OSError``，调用方按"缓存写入失败"收尾——已生成的图
        由门面决定是否以 data_url 形式就地返还（不浪费已计费的请求）。
        """
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("单张图片超过 20 MB 缓存上限")
        _validate_key(key)
        if str(ext).lower() not in _IMAGE_EXTENSIONS:
            raise ValueError("缓存图片扩展名无效")
        storage.ensure_dir(self.base)
        target = self.image_path(key, str(ext).lower())
        _atomic_write_bytes(target, data)
        try:
            _atomic_write_bytes(
                self.meta_path(key),
                json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            )
        except BaseException:
            with _suppress_os():
                target.unlink(missing_ok=True)  # 回滚：不留没有 sidecar 的孤儿图
            raise
        self.prune(preserve_key=key)
        return target

    def latest(self) -> tuple[str, dict[str, Any], Path] | None:
        """按 sidecar 的 ``created_at`` 找最近一张；目录空 / 全坏返回 ``None``。

        每次全扫 sidecar：缓存里通常只有几张到几十张，量级可忽略，不值得再加索引。
        """
        best: tuple[str, dict[str, Any], Path] | None = None
        try:
            entries = list(self.base.glob(f"*{_META_SUFFIX}"))
        except OSError:
            return None
        for meta_path in entries:
            doc = storage.read_json(meta_path, default=None)
            if not isinstance(doc, dict) or not isinstance(doc.get("key"), str):
                continue
            try:
                key = _validate_key(str(doc["key"]))
                path = self.find(key)
            except ValueError:
                continue
            if path is None:
                continue
            created = str(doc.get("created_at") or "")
            if best is None or created >= str(best[1].get("created_at") or ""):
                best = (key, doc, path)
        return best

    def prune(self, *, preserve_key: str | None = None) -> int:
        """Bound owned cache files by item count and bytes, retaining the newest valid image.

        Only direct children matching this cache's 32-hex key and known extensions are eligible.
        Arbitrary files, directories, and similarly named backups are left untouched.
        """
        try:
            entries = list(self.base.iterdir())
        except OSError:
            return 0
        groups: dict[str, dict[str, Path]] = {}
        for path in entries:
            if not path.is_file():
                continue
            match = _OWNED_FILE_RE.fullmatch(path.name)
            if match:
                groups.setdefault(match.group(1), {})[match.group(2)] = path

        records: list[tuple[str, str, list[Path], int, bool]] = []
        for key, files in groups.items():
            image = next((files[ext] for ext in _IMAGE_EXTENSIONS if ext in files), None)
            meta = files.get(_META_SUFFIX)
            owned = list(files.values())
            total = sum(_safe_size(path) for path in owned)
            created = ""
            valid = image is not None and meta is not None
            if valid:
                doc = storage.read_json(meta, default=None)
                valid = isinstance(doc, dict) and doc.get("key") == key
                if valid:
                    created = str(doc.get("created_at") or "")
            records.append((key, created, owned, total, valid))
        if not records:
            return 0

        # The newest valid pair is the last successful result and is always retained.
        valid_records = [record for record in records if record[4]]
        newest = max(valid_records, key=lambda record: record[1]) if valid_records else None
        ordered = sorted(
            records,
            key=lambda record: (record[0] == preserve_key, record is newest, record[1]),
            reverse=True,
        )
        kept_bytes = 0
        removed = 0
        kept_items = 0
        for key, _created, files, total, valid in ordered:
            # Keep complete image/sidecar pairs only; owned crash orphans are safe to remove.
            if not valid:
                should_keep = False
            else:
                should_keep = (
                    kept_items < MAX_CACHE_IMAGES
                    and kept_bytes + total <= MAX_CACHE_BYTES
                )
                if key == preserve_key or (key, _created, files, total, valid) == newest:
                    should_keep = True
            if should_keep:
                kept_items += 1
                kept_bytes += total
                continue
            for path in files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            removed += 1
        return removed

    def cleanup_tmp(self) -> int:
        """清掉生成中途死掉的 ``.part`` 残骸，返回清理数（启动时调用）。

        ``_atomic_write_bytes`` 的临时文件是 ``.<名字>.part-<随机数>``（``mkstemp``
        后缀带随机数），所以按 ``*part*`` 前缀匹配而不是精确后缀。
        """
        count = 0
        try:
            entries = [p for p in self.base.iterdir() if p.is_file() and _TMP_RE.fullmatch(p.name)]
        except OSError:
            return 0
        for path in entries:
            try:
                path.unlink()
                count += 1
            except OSError:
                pass
        return count


class _suppress_os:
    """``contextlib.suppress(OSError)`` 的极简替身（避免只为一条 import 拉整个 contextlib）。"""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return exc_type is not None and issubclass(exc_type, OSError)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写字节文件：与 ``webui_portrait._atomic_write_bytes`` 同源（同目录临时文件 → replace）。"""
    path = Path(path)
    storage.ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}{TMP_SUFFIX}", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with _suppress_os():
            os.unlink(tmp)
        raise


__all__ = ["MAX_IMAGE_BYTES", "TMP_SUFFIX", "ImageCache", "key_for"]


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
