"""状态系统的文件层：``state.json``（情绪）与 ``affinity.json``（熟悉度）的读写。

两张 JSON 共用一套契约（第 0 步定稿）：原子写、自动 ``schema_version``、mtime 缓存读、
每文件一把 ``asyncio.Lock``。变更走 ``update_*``：把"读-改-写"整体关进锁内，
闭包只做纯计算（衰减、限幅），保证并发改动不互相覆盖。

``state_history.jsonl``（情绪历史，任务书 §2.5 v0.7）是**追加型**文件，一行一个 JSON，
是 WebUI 情绪曲线的唯一数据源——文件路径与行形状就是接口，键名冻结
（``ts`` / ``layer`` / ``valence`` / ``arousal`` / ``word``），没有 ``schema_version`` 信封。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import storage
from .._log import logger

HISTORY_KEEP_DAYS = 180
"""情绪历史的保留天数（任务书 §2.5#4：模块常量，不做配置项）。"""

LAYER_NOW = "now"
LAYER_BASELINE = "baseline"

AFFINITY_FLUSH_SECONDS = 1.5
"""熟悉度的合并写窗口（秒，返工轮定稿）：同会话连续互动只落一次盘（框架 §4.3 写放大）。

窗口内的增量只活在内存 overlay 里，由后台任务等窗口安静后统一原子落盘；
正常关闭（适配器 ``terminate``）会强制 flush。硬断电最多丢窗口内（≤1.5 秒）
的互动增量——合并写之下"断电不丢"的语义：**不设更长的内存累积期**。
"""


@dataclass
class StateStore:
    """``state.json``（她的情绪，全局一条）与 ``state_history.jsonl``（情绪曲线）的位置与变更。"""

    layout: storage.Layout
    locks: storage.KeyedLocks

    def path(self) -> Path:
        return self.layout.state

    def history_path(self) -> Path:
        return self.layout.state_history

    # ---- state.json ----------------------------------------------------------

    def read(self) -> dict[str, Any]:
        """读整张（走 mtime 缓存）。缺键补齐。"""
        doc = storage.read_json(self.path(), default={"mood": {}})
        doc.setdefault("mood", {})
        return doc

    def read_mood(self) -> dict[str, Any]:
        return dict(self.read().get("mood") or {})

    async def update_mood(self, update: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """在锁内改情绪并落盘，返回改完的 ``mood``。``update`` 必须是纯函数。"""
        async with self.locks.acquire(self.path()):
            doc = self.read()
            mood = update(dict(doc.get("mood") or {}))
            doc["mood"] = mood
            self._save(doc)
            return dict(mood)

    def _save(self, doc: dict[str, Any]) -> None:
        storage.atomic_write_json(self.path(), doc)
        storage.shared_cache.invalidate(self.path())

    # ---- state_history.jsonl ---------------------------------------------------

    def read_history_lines(self) -> list[dict[str, Any]]:
        """读全部历史行（走 mtime 缓存）。坏行丢弃，不抛错。"""
        text = storage.shared_cache.read_text(self.history_path(), default="")
        lines: list[dict[str, Any]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                lines.append(parsed)
        return lines

    async def append_history(self, lines: list[dict[str, Any]], *, now: datetime) -> None:
        """追加若干历史行；最老的好行超过 ``HISTORY_KEEP_DAYS`` 时整文件重写一次（唯一重写时机）。

        正常路径是纯追加（单行小写入）；裁剪路径走 ``atomic_write_text``（§2.5#4）。
        """
        payload = [dict(line) for line in lines if isinstance(line, dict)]
        if not payload:
            return
        async with self.locks.acquire(self.history_path()):
            existing = self.read_history_lines()
            cutoff = now - timedelta(days=HISTORY_KEEP_DAYS)
            keep = [line for line in existing if _line_ts(line) is None or _line_ts(line) >= cutoff]
            stale = len(keep) < len(existing)
            if stale:
                # 超期旧行清掉：整文件重写（这是唯一的重写时机，§2.5#4）
                body = "".join(
                    json.dumps(line, ensure_ascii=False) + "\n" for line in keep + payload
                )
                storage.atomic_write_text(self.history_path(), body)
                storage.shared_cache.invalidate(self.history_path())
                return
            with open(
                self.history_path(), "a", encoding="utf-8", newline=""
            ) as handle:
                for line in payload:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            storage.shared_cache.invalidate(self.history_path())


@dataclass
class AffinityStore:
    """``affinity.json``（每人一个漏积分器标量）的位置与变更。

    写路径带 **合并写窗口**（``AFFINITY_FLUSH_SECONDS``）：``update_person`` 只改
    内存 overlay，等窗口安静后由后台任务统一原子落盘；``read_people`` 恒返回
    "文件 + overlay" 的最新值——本进程内的读写永远一致，磁盘最多滞后一个窗口。
    """

    layout: storage.Layout
    locks: storage.KeyedLocks
    _pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    _flush_task: asyncio.Task | None = None

    def path(self) -> Path:
        return self.layout.affinity

    def read(self) -> dict[str, Any]:
        doc = storage.read_json(self.path(), default={"people": {}})
        doc.setdefault("people", {})
        return doc

    def read_people(self) -> dict[str, dict[str, Any]]:
        people = {
            str(key): dict(value or {})
            for key, value in (self.read().get("people") or {}).items()
        }
        for person_id, person in self._pending.items():  # 未落盘的增量盖在文件值上
            people[str(person_id)] = dict(person)
        return people

    async def update_person(
        self, person_id: str, update: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> dict[str, Any]:
        """在锁内改一个人（先落内存 overlay），窗口安静后合并落盘。``update`` 必须是纯函数。

        连续多次 ``touch`` 的数学效果与写穿完全一致：闭包的输入永远是
        "文件值 + overlay" 的最新值，衰减与封顶都按最新状态连续演化。
        """
        async with self.locks.acquire(self.path()):
            base = dict(self.read_people().get(person_id) or {})
            person = update(base)
            self._pending[str(person_id)] = person
        self._ensure_flush_task()
        return dict(person)

    async def flush(self) -> bool:
        """把窗口内的增量合并进 ``affinity.json``（原子写）。没有增量返回 ``False``。

        换出 overlay 与落盘**全程持同一把锁**：中途到来的 ``update_person`` 要么
        排在后面（读到已落盘的新值），要么被本次 flush 一并带走——不会丢增量。
        """
        async with self.locks.acquire(self.path()):
            if not self._pending:
                return False
            pending = self._pending
            self._pending = {}
            doc = self.read()
            people = {
                str(key): dict(value or {})
                for key, value in (doc.get("people") or {}).items()
            }
            people.update(pending)
            doc["people"] = people
            self._save(doc)
            return True

    def _ensure_flush_task(self) -> None:
        """排一个"窗口安静后落盘"的后台任务；已有活着的任务就不重复（它的循环会兜住新增量）。"""
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.get_running_loop().create_task(self._flush_when_quiet())
        except RuntimeError:  # 没有 running loop（同步上下文）；下次 async 调用再排
            pass

    async def _flush_when_quiet(self) -> None:
        try:
            while True:
                await asyncio.sleep(AFFINITY_FLUSH_SECONDS)
                if not await self.flush():
                    return
        except Exception:  # noqa: BLE001 - 落盘失败不能带累互动计数：增量留在 overlay，下次重试
            logger.exception("affinity.json 合并落盘失败（增量保留在内存，下次 touch 重试）")
        finally:
            self._flush_task = None

    def _save(self, doc: dict[str, Any]) -> None:
        storage.atomic_write_json(self.path(), doc)
        storage.shared_cache.invalidate(self.path())


def _line_ts(line: dict[str, Any]) -> datetime | None:
    text = str(line.get("ts") or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = [
    "AFFINITY_FLUSH_SECONDS",
    "HISTORY_KEEP_DAYS",
    "LAYER_BASELINE",
    "LAYER_NOW",
    "AffinityStore",
    "StateStore",
]
