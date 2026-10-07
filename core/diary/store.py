"""日记的文件层：两本的位置、追加、按行局部替换、改删前备份、每文件一把锁。

写文件一律走 ``core.storage`` 的原子写：**要么整段成功，要么什么都不变**。
追加不用 `open(..., "a")`：读-改-写的原子性更可控，也顺手保证"文件以换行结尾"
（这条是日记格式的不变式，见 ``format`` 模块头）。

**两种备份，两种语义**（别混）：

1. ``backup()`` 写的 ``{stamp}-{action}-{book}.txt`` 是**整本快照**，给 ``diary_edit``
   工具用——她改错了要人工回捞。整本回写会连带抹掉这之后的写入，所以它**不能**做成一键还原。
2. 第 15 步的 ``archive_segment()`` 写的 ``seg-{stamp}-{book}.json`` 是**单段记录**，
   面板删一段就存一段，还原时按时间顺序插回它原本该在的位置，不碰其余内容。
   回收站只认这一类。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from .. import storage
from . import format as fmt

TRASH_SEGMENT_PREFIX = "seg-"
"""回收站**单段记录**的文件名前缀。与 ``backup()`` 的整本快照（无前缀）分开，
回收站只列 ``seg-`` 开头的 ``.json`` —— 整本快照不是可一键还原的东西，摆进回收站
会骗主人点一个按下去反而抹掉后来日记的按钮。"""

TRASH_SEGMENT_CAP = 200
"""回收站上限（条）。超了丢最旧的：回收站是**兜底**不是仓库，真要长期留着就该
先把那段挪到别处，而不是让这里无限长。"""


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

    # ---- 回收站：按段归档 + 还原（第 15 步）----------------------------------

    def trash_items(self) -> list[dict]:
        """回收站条目，**按删除时间倒序**（新的在前）。

        读不到的 / 形状不对的文件直接跳过：回收站是给人看的目录，一个坏文件不该
        让整页打不开。返回的每一项都带 ``id``（文件名去后缀），还原只认这个。
        """
        items: list[dict] = []
        try:
            paths = sorted(self.layout.trash_dir.glob(f"{TRASH_SEGMENT_PREFIX}*.json"))
        except OSError:
            return []
        for path in paths:
            raw = storage.read_text(path, "")
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or not record.get("date"):
                continue
            record["id"] = path.stem
            try:
                record["bytes"] = int(path.stat().st_size)
            except OSError:
                record["bytes"] = 0
            items.append(record)
        items.sort(key=lambda item: str(item.get("deleted_at") or item.get("id") or ""), reverse=True)
        return items

    def trash_record(self, trash_id: str) -> dict | None:
        """按 ``id`` 取一条归档记录；取不到返回 ``None``。"""
        key = str(trash_id or "").strip()
        if not key or Path(key).name != key or not key.startswith(TRASH_SEGMENT_PREFIX):
            return None
        raw = storage.read_text(self.layout.trash_dir / f"{key}.json", "")
        if not raw:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or not record.get("date"):
            return None
        record["id"] = key
        return record

    def archive_segment(self, book: str, seg: fmt.Segment, *, now: datetime) -> str:
        """把**一段**存进回收站，返回它的 ``id``（失败返回空串）。

        与 ``backup()`` 的整本快照是两条线：这里存的是"这一段"，还原时只把它插回去。
        """
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        trash_id = f"{TRASH_SEGMENT_PREFIX}{stamp}-{book}"
        record = {
            "book": book,
            "date": seg.date,
            "time": seg.time,
            "mood": seg.mood,
            "who": seg.who,
            "text": seg.text,
            "seg_id": fmt.seg_id(seg),
            "deleted_at": now.isoformat(timespec="seconds"),
        }
        storage.atomic_write_text(
            self.layout.trash_dir / f"{trash_id}.json",
            json.dumps(record, ensure_ascii=False, indent=2),
        )
        self._prune_trash()
        return trash_id

    def _prune_trash(self) -> None:
        """超上限就丢最旧的（尽力而为；删不掉不阻塞主流程）。"""
        try:
            paths = sorted(self.layout.trash_dir.glob(f"{TRASH_SEGMENT_PREFIX}*.json"))
        except OSError:
            return
        for path in paths[:-TRASH_SEGMENT_CAP] if len(paths) > TRASH_SEGMENT_CAP else []:
            try:
                os.unlink(path)
            except OSError:
                continue

    async def apply_by_id(
        self,
        book: str,
        *,
        seg_id: str,
        action: str,
        new_body: str = "",
        now: datetime,
        within_days: int = 0,
        tz: tzinfo,
    ) -> fmt.EditResult:
        """按 ``seg_id`` 精确改 / 删一段（面板路径）。

        ``within_days <= 0`` 表示**不受窗口限制**（面板豁免开关打开时走这条）；否则按
        ``diary.edit_within_days`` 判"太旧"——和 ``diary_edit`` 工具同一口径。
        未来段（时钟没同步对的那种）**两种模式都拒**，豁免的是"多久以前"，不是"没发生"。

        顺序上先写回收站再落盘：归档失败**不**阻断删除（正文已经没了，不能反过来
        因为存不下备份就删不掉），但那种情况会写日志，主人至少能知道没得还原。
        """
        path = self.path(book)
        async with self.locks.acquire(path):
            current = storage.read_text(path)
            segments = fmt.split_segments(current)
            seg = fmt.find_by_id(segments, seg_id)
            if seg is None:
                return fmt.EditResult(ok=False, error="找不到这一段（可能已经删过，或内容变过了）")
            if not fmt.already_happened(seg, now, tz):
                return fmt.EditResult(
                    ok=False,
                    error=f"{seg.stamp} 那段的时间还没到（多半是时钟没同步），不动它",
                )
            if within_days > 0 and not fmt.within_window(seg, now, within_days, tz):
                return fmt.EditResult(
                    ok=False,
                    error=f"{seg.stamp} 那段太旧了（只能改最近 {within_days} 天的）；"
                    "要在面板里改任意一段，去设置打开「面板不受可改天数限制」",
                )
            if action == "delete":
                result = fmt.delete_segment_at(current, seg)
            elif action == "rewrite":
                result = fmt.rewrite_segment_at(current, seg, new_body)
            else:
                return fmt.EditResult(ok=False, error="action 只能是 rewrite 或 delete")
            if not result.ok:
                return result
            self.backup(book, current, action=action, now=now)
            if action == "delete":
                try:
                    self.archive_segment(book, seg, now=now)
                except OSError:
                    pass
            storage.atomic_write_text(path, result.text)
            storage.shared_cache.invalidate(path)
            return result

    async def restore_segment(self, trash_id: str, *, now: datetime) -> tuple[bool, str]:
        """从回收站还原一段，返回 ``(是否成功, 原因)``。

        幂等：同一份归档还原两次，第二次会认出"这段已经在本子里了"并拒绝，
        **绝不**插出两条一样的。头行按 ``make_head_line`` 重建——它由这四个字段
        唯一决定，重建结果与原来那句逐字相同（写入侧与解析侧同源，见模块头不变式 1）。
        """
        record = self.trash_record(trash_id)
        if record is None:
            return False, "找不到这一条（可能已经还原过了）"
        book = str(record.get("book") or "")
        if book not in fmt.BOOKS:
            return False, "这一条记的是哪本都认不出来，放着别动"
        seg = fmt.segment_from_record(record)
        if not seg.text:
            return False, "这一条没有正文，放着别动"
        path = self.path(book)
        async with self.locks.acquire(path):
            current = storage.read_text(path)
            if fmt.find_by_id(fmt.split_segments(current), fmt.seg_id(seg)) is not None:
                return False, "这一段已经在本子里了，不用再还原"
            storage.atomic_write_text(path, fmt.insert_segment(current, seg))
            storage.shared_cache.invalidate(path)
        try:
            os.unlink(self.layout.trash_dir / f"{trash_id}.json")
        except OSError:
            pass  # 还原成功但清不掉归档条目：顶多多列一条，可再次还原（幂等挡着，不会插重）
        return True, ""



__all__ = ["TRASH_SEGMENT_CAP", "TRASH_SEGMENT_PREFIX", "DiaryStore"]
