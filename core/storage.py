"""存储契约（第 0 步产物）。

本模块定义「陪伴系统」的全部落盘行为，五个使用方（日记 / 小本本 / 状态 / 熟悉度 / WebUI）共用：

1. **目录与文件名固定**（``Layout``）——WebUI 是独立进程，靠这套名字直接读文件；
2. **写入原子**——同目录临时文件 → ``os.replace``，失败不留半截文件；
3. **编码与换行固定**——UTF-8（不带 BOM）、行尾恒为 ``\\n``；写文本一律 ``newline=""``，
   禁止 Windows 把 ``\\n`` 翻成 ``\\r\\n``（日记格式以 ``\\n`` 为不变式）；
4. **读取带 mtime 缓存**（``FileCache``）——外部进程改了文件能感知，又不每次请求都解析；
5. **JSON 带 ``schema_version``**——比本程序新的文件只读不改写，避免旧代码毁掉新数据。

只依赖标准库；不 import astrbot，可离线测试。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---- 契约常量 ----------------------------------------------------------------

SCHEMA_VERSION = 1
"""JSON 数据文件的当前结构版本。改动字段语义时 +1，并在 ``MIGRATIONS`` 里补上一级。"""

TEXT_ENCODING = "utf-8"
LINE_SEP = "\n"

DIARY_FILE = "日记.txt"
LOVE_DIARY_FILE = "恋爱日记.txt"
NOTEBOOK_FILE = "notebook.json"
STATE_FILE = "state.json"
STATE_HISTORY_FILE = "state_history.jsonl"
AFFINITY_FILE = "affinity.json"
PROACTIVE_FILE = "proactive.json"
PROACTIVE_LOG_FILE = "proactive-log.jsonl"
NAMES_FILE = "names.json"
DIARY_NUDGE_FILE = "diary-nudge.json"
TRASH_DIR = "diary-trash"

JSON_FILES = (
    NOTEBOOK_FILE,
    STATE_FILE,
    AFFINITY_FILE,
    PROACTIVE_FILE,
    NAMES_FILE,
    DIARY_NUDGE_FILE,
)
"""带 ``schema_version`` 的 JSON 文件（日记正文是纯文本、情绪历史与主动消息记录是 JSONL 追加文件，都不带版本头）。"""

_REPLACE_RETRIES = 6
_REPLACE_DELAY = 0.02


# ---- 目录布局 ----------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """数据目录布局。``base_dir`` 由宿主在运行时给出（AstrBot：``StarTools.get_data_dir(插件名)``）。

    日记正文是纯文本 ``.txt``（产品主张：记事本能直接打开）；其余系统用 JSON。
    """

    base_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_dir", Path(self.base_dir))

    @property
    def diary(self) -> Path:
        return self.base_dir / DIARY_FILE

    @property
    def love_diary(self) -> Path:
        return self.base_dir / LOVE_DIARY_FILE

    @property
    def notebook(self) -> Path:
        return self.base_dir / NOTEBOOK_FILE

    @property
    def state(self) -> Path:
        return self.base_dir / STATE_FILE

    @property
    def state_history(self) -> Path:
        return self.base_dir / STATE_HISTORY_FILE

    @property
    def affinity(self) -> Path:
        return self.base_dir / AFFINITY_FILE

    @property
    def proactive(self) -> Path:
        return self.base_dir / PROACTIVE_FILE

    @property
    def proactive_log(self) -> Path:
        return self.base_dir / PROACTIVE_LOG_FILE

    @property
    def names(self) -> Path:
        return self.base_dir / NAMES_FILE

    @property
    def diary_nudge(self) -> Path:
        return self.base_dir / DIARY_NUDGE_FILE

    @property
    def trash_dir(self) -> Path:
        return self.base_dir / TRASH_DIR

    def files(self) -> tuple[Path, ...]:
        """全部数据文件（不含目录）。"""
        return (
            self.diary,
            self.love_diary,
            self.notebook,
            self.state,
            self.state_history,
            self.affinity,
            self.proactive,
            self.proactive_log,
            self.names,
            self.diary_nudge,
        )

    def ensure(self) -> Layout:
        """建好目录（含回收站）。返回自身，便于链式调用。"""
        ensure_dir(self.base_dir)
        ensure_dir(self.trash_dir)
        return self


# ---- 原子写 ------------------------------------------------------------------


def ensure_dir(path: Path | str) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path | str, text: str) -> None:
    """原子写文本：同目录临时文件 → ``os.replace``。

    写出的文件是 UTF-8 无 BOM、行尾原样（不传 newline 转换），权限继承 ``mkstemp`` 的 0600。
    ``os.replace`` 在 Windows 上遇到读者占用会抛 ``PermissionError``，这里做有限重试。
    """
    path = Path(path)
    ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=TEXT_ENCODING, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    """原子写 JSON，并盖上当前 ``schema_version``（调用方不用自己带）。"""
    doc = dict(payload)
    doc["schema_version"] = SCHEMA_VERSION
    atomic_write_text(
        path,
        json.dumps(doc, ensure_ascii=False, indent=2) + LINE_SEP,
    )


def _replace_with_retry(tmp: str, path: Path) -> None:
    last: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:  # Windows: 目标被读者占用
            last = exc
            time.sleep(_REPLACE_DELAY * (attempt + 1))
    assert last is not None
    raise last


# ---- 读 ----------------------------------------------------------------------


def read_text(path: Path | str, default: str = "") -> str:
    """读文本（不翻译换行，读出原文）。文件不存在返回 ``default``；其他 IO 错误同样降级。"""
    try:
        with open(path, "r", encoding=TEXT_ENCODING, newline="") as handle:
            return handle.read()
    except FileNotFoundError:
        return default
    except OSError as exc:
        logger.warning("读取失败，按缺省处理：%s（%s）", path, exc)
        return default


@dataclass
class _CacheEntry:
    mtime_ns: int
    size: int
    text: str


@dataclass
class FileCache:
    """按 ``(mtime_ns, size)`` 判失效的文本缓存。

    - 进程内**只**缓存文本，不缓存派生结果；
    - 文件被外部进程原子替换后 ``mtime_ns`` 变化 → 自动重读；
    - ``hits`` / ``misses`` 供测试与自检观察。
    """

    _text: dict[str, _CacheEntry] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def read_text(self, path: Path | str, default: str = "") -> str:
        key = os.path.abspath(str(path))
        stat = _stat_signature(key)
        if stat is None:
            self._text.pop(key, None)
            self.misses += 1
            return default
        entry = self._text.get(key)
        if entry is not None and (entry.mtime_ns, entry.size) == stat:
            self.hits += 1
            return entry.text
        self.misses += 1
        text = read_text(key, default)
        self._text[key] = _CacheEntry(mtime_ns=stat[0], size=stat[1], text=text)
        return text

    def read_json(
        self,
        path: Path | str,
        default: Mapping[str, Any] | None = None,
        migrations: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        return load_json(self.read_text(path), default=default, migrations=migrations)

    def invalidate(self, path: Path | str | None = None) -> None:
        if path is None:
            self._text.clear()
            return
        self._text.pop(os.path.abspath(str(path)), None)

    def clear(self) -> None:
        self.invalidate(None)


def _stat_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


shared_cache = FileCache()
"""默认缓存实例：各系统的 store 直接复用它，保证「同一份文件只解析一次」。"""


def read_json(
    path: Path | str,
    default: Mapping[str, Any] | None = None,
    migrations: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """读 JSON（走 ``shared_cache``）。见 ``load_json`` 的版本策略。"""
    return shared_cache.read_json(path, default=default, migrations=migrations)


def load_json(
    text: str,
    default: Mapping[str, Any] | None = None,
    migrations: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """把文本解析成带版本语义的字典。

    版本策略（全部**不抛异常**，最坏情况是降级）：

    - 空文本 / 坏 JSON / 顶层不是对象 → 返回 ``default`` 的副本（默认 ``{}``）；
    - ``schema_version`` 缺失 → 当作 1（本步冻结的初版）；
    - 比当前旧 → 依次调用 ``migrations[旧版本]`` 升级；缺对应迁移则**原样返回**并记警告；
    - 比当前新 → **原样返回，绝不改写**（旧代码不毁新数据），记警告。
    """
    fallback = dict(default) if default else {}
    raw = (text or "").strip()
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("JSON 解析失败，按缺省处理：%s", exc)
        return fallback
    if not isinstance(parsed, dict):
        logger.warning("JSON 顶层不是对象，按缺省处理")
        return fallback
    return migrate_doc(parsed, migrations=migrations)


def migrate_doc(
    doc: dict[str, Any],
    migrations: Mapping[int, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """按 ``schema_version`` 升级一个已解析的文档。见 ``load_json`` 的策略说明。"""
    version = doc.get("schema_version", 1)
    if not isinstance(version, int):
        logger.warning("schema_version 不是整数（%r），当作 %d", version, SCHEMA_VERSION)
        version = SCHEMA_VERSION
    if version > SCHEMA_VERSION:
        logger.warning(
            "数据文件版本 %d 比本程序(%d)新，按原样使用、不写入", version, SCHEMA_VERSION
        )
        return doc
    while version < SCHEMA_VERSION:
        step = (migrations or {}).get(version)
        if step is None:
            logger.warning(
                "缺少 v%d → v%d 的迁移，原样使用（可能缺字段）", version, SCHEMA_VERSION
            )
            return doc
        doc = step(doc)
        version = doc.get("schema_version", version + 1)
        if not isinstance(version, int):
            version = SCHEMA_VERSION
    return doc


# ---- 进程内串行（不跨进程） ---------------------------------------------------


@dataclass
class KeyedLocks:
    """每个 key（一般用文件路径）一把 ``asyncio.Lock``。

    ``asyncio.Lock`` **只保护本进程**；WebUI 是外部进程，靠「它也原子写 + 我们按 mtime 重读」
    协作，不能指望这把锁（见方案 §4.5）。
    """

    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, key: Path | str) -> asyncio.Lock:
        name = os.path.abspath(str(key)) if os.sep in str(key) else str(key)
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    @contextlib.asynccontextmanager
    async def acquire(self, key: Path | str) -> Iterator[asyncio.Lock]:
        lock = self.lock_for(key)
        async with lock:
            yield lock


__all__ = [
    "AFFINITY_FILE",
    "DIARY_FILE",
    "DIARY_NUDGE_FILE",
    "JSON_FILES",
    "LINE_SEP",
    "LOVE_DIARY_FILE",
    "NAMES_FILE",
    "NOTEBOOK_FILE",
    "PROACTIVE_FILE",
    "SCHEMA_VERSION",
    "STATE_FILE",
    "STATE_HISTORY_FILE",
    "TEXT_ENCODING",
    "TRASH_DIR",
    "FileCache",
    "KeyedLocks",
    "Layout",
    "atomic_write_json",
    "atomic_write_text",
    "ensure_dir",
    "load_json",
    "migrate_doc",
    "read_json",
    "read_text",
    "shared_cache",
]
