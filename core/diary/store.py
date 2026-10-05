"""日记的文件层：两本的位置、追加、按行局部替换、改删前备份、每文件一把锁。

写文件一律走 ``core.storage`` 的原子写：**要么整段成功，要么什么都不变**。
追加不用 `open(..., "a")`：读-改-写的原子性更可控，也顺手保证"文件以换行结尾"
（这条是日记格式的不变式，见 ``format`` 模块头）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from .. import storage
from . import format as fmt


@dataclass
class DiaryStore:
    layout: storage.Layout
    locks: storage.KeyedLocks

    def path(self, book: str) -> Path:
        return self.layout.love_diary if book == fmt.LOVE else self.layout.diary

    def exists(self, book: str) -> bool:
        return self.path(book).is_file()

    def read(self, book: str) -> str:
        return storage.read_text(self.path(book))

    async def append(
        self,
        book: str,
        *,
        date: str,
        time: str,
        mood: object = "",
        tag: object = "",
        body: str,
    ) -> None:
        """追加一段。头行由 ``format.make_head_line`` 生成（写入侧与正则同源）。"""
        path = self.path(book)
        head = fmt.make_head_line(date, time, mood, tag)
        async with self.locks.acquire(path):
            current = storage.read_text(path)
            chunk = f"{head}\n{body}\n\n"
            if current and not current.endswith("\n"):
                chunk = "\n" + chunk  # 兜底：被手改过的文件结尾没换行
            storage.atomic_write_text(path, current + chunk)
            storage.shared_cache.invalidate(path)

    async def save(self, book: str, text: str) -> None:
        path = self.path(book)
        async with self.locks.acquire(path):
            storage.atomic_write_text(path, text)
            storage.shared_cache.invalidate(path)

    def backup(
        self, book: str, text: str, *, action: str, now: datetime
    ) -> Path | None:
        """把原文丢进 ``diary-trash/``。失败不阻塞（只影响可回滚性）。"""
        if not text:
            return None
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        target = self.layout.trash_dir / f"{stamp}-{action}-{book}.txt"
        try:
            storage.atomic_write_text(target, text)
        except OSError:
            return None
        return target

    async def apply(
        self,
        book: str,
        *,
        action: str,
        match: str,
        new_body: str,
        now: datetime,
        within_days: int,
        tz: tzinfo,
    ) -> fmt.EditResult:
        """改 / 删一段：**先备份原文，再落盘**（顺序不能反）。"""
        path = self.path(book)
        async with self.locks.acquire(path):
            current = storage.read_text(path)
            if action == "delete":
                result = fmt.delete_segment(
                    current, match=match, now=now, within_days=within_days, tz=tz
                )
            else:
                result = fmt.rewrite_segment(
                    current,
                    match=match,
                    new_body=new_body,
                    now=now,
                    within_days=within_days,
                    tz=tz,
                )
            if not result.ok:
                return result
            self.backup(book, current, action=action, now=now)
            storage.atomic_write_text(path, result.text)
            storage.shared_cache.invalidate(path)
            return result


__all__ = ["DiaryStore"]
