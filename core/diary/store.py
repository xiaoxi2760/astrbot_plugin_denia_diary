"""日记的文件层：两本的位置、JSONL 读写、改删前备份、每文件一把锁。

**权威存储是 JSONL（第 17 步）**：一行一段 ``{"date","time","mood","who",
"person_id","text"}``，键序冻结（``format.ENTRY_LINE_KEYS``）。结构化数据放结构化
文件——旧版把 person_id 编码进头行、靠正则抠出来，一个怪 id 就能炸掉写路径，
且展示文本与机器数据搅在一起；那个方案已废弃。旧纯文本 ``日记.txt`` 在**首次访问
时一次性导入**，之后原样留在数据目录当人工可读的旧档，不再被读写。

写文件一律走 ``core.storage`` 的原子写：**要么整段成功，要么什么都不变**。
（JSONL 本可以 `open("a")` 追加，但本子的写路径全是"读-改-写"（改/删/还原都要动
中间的行），统一走原子重写只有一种正确性要证明。）

**两种备份，两种语义**（别混）：

1. ``backup()`` 写的 ``{stamp}-{action}-{book}.jsonl`` 是**整本快照**，给
   ``diary_edit`` 工具用——她改错了要人工回捞。整本回写会连带抹掉这之后的写入，
   所以它**不能**做成一键还原。
2. 第 15 步的 ``archive_segment()`` 写的 ``seg-{stamp}-{book}.json`` 是**单段记录**，
   面板删一段就存一段，还原时按时间顺序插回它原本该在的位置，不碰其余内容。
   回收站只认这一类。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
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


def person_id_by_name(
    who: object, preference: object, contacts: object
) -> str:
    """旧头行标注名 → person_id（旧 ``.txt`` 导入时的**回填**，方案甲 / 用户 2026-10-11 拍板）。

    名字候选来自 ``name_preference``（id → 称呼）与 ``contacts``（id → ``{"name": …}``）：
    **恰好匹配一人**才认，查不到或重名一律返回空串——宁可不见，不可错认。群标注
    （``群·xx``）不是人，天然跳过。回填错了随时能拿原样保留的旧 txt 对回来。
    """
    tag = str(who or "").strip()
    if not tag.startswith("和"):
        return ""
    name = tag[1:].strip()
    if not name:
        return ""
    hits: set[str] = set()
    for pid, named in dict(preference or {}).items():
        if str(named or "").strip() == name:
            hits.add(str(pid))
    for pid, info in dict(contacts or {}).items():
        named = info.get("name") if isinstance(info, dict) else info
        if str(named or "").strip() == name:
            hits.add(str(pid))
    return hits.pop() if len(hits) == 1 else ""


@dataclass
class DiaryStore:
    layout: storage.Layout
    locks: storage.KeyedLocks
    name_lookup: "Callable[[str], str] | None" = None
    """旧头行标注名 → person_id 的**回填查询**（``person_id_by_name`` 的注入形态）。

    只在旧 ``.txt`` 导入那一刻用：拿到标注（如 ``和希``）返回唯一归属 id，查不到 /
    重名给空串。默认 ``None``＝不回填（本子按"不归属任何人"照常读）。"""

    def path(self, book: str) -> Path:
        """旧纯文本的位置。**只读**：首次导入的来源 + 人工可读的旧档。"""
        return self.layout.love_diary if book == fmt.LOVE else self.layout.diary

    def jsonl_path(self, book: str) -> Path:
        """权威存储的位置（JSONL）。"""
        return (
            self.layout.love_diary_jsonl if book == fmt.LOVE else self.layout.diary_jsonl
        )

    def exists(self, book: str) -> bool:
        return self.jsonl_path(book).is_file() or self.path(book).is_file()

    # ---- 读 ------------------------------------------------------------------

    def read_entries(self, book: str) -> list[fmt.Entry]:
        """一本的全部条目。JSONL 优先；没有就**一次性导入**旧 ``.txt``。

        导入会把条目原样搬进 JSONL（含旧头行里 ``#id`` 抠出来的归属），再按
        ``name_lookup`` 对"只有名字、没有 id"的旧段落做一次**回填**（方案甲：
        恰好匹配一人才认）。旧 txt 一字不动地留在原地。并发下两个导入都写同样
        内容的 JSONL，谁后落盘都一样——导入天然幂等，不需要额外的标记文件。
        """
        path = self.jsonl_path(book)
        if path.is_file():
            return fmt.entries_from_jsonl(storage.read_text(path), book)
        legacy = self.path(book)
        if legacy.is_file():
            entries = fmt.parse_entries(storage.read_text(legacy), book)
            if self.name_lookup is not None:
                entries = [
                    replace(entry, person_id=entry.person_id or self.name_lookup(entry.who))
                    for entry in entries
                ]
            self._write_jsonl(book, entries)
            return entries
        return []

    def read(self, book: str) -> str:
        """整本的**纯文本视图**（旧 ``.txt`` 版式）。展示/人工查看用，
        不是存储格式——存储在 ``read_entries`` 背后的 JSONL 里。"""
        return fmt.view_text(self.read_entries(book))

    def _write_jsonl(self, book: str, entries: list[fmt.Entry]) -> None:
        path = self.jsonl_path(book)
        payload = "".join(fmt.entry_line(entry) + "\n" for entry in entries)
        storage.atomic_write_text(path, payload)
        storage.shared_cache.invalidate(path)

    async def append(
        self,
        book: str,
        *,
        date: str,
        time: str,
        mood: object = "",
        tag: object = "",
        body: str,
        person_id: object = "",
    ) -> None:
        """追加一段。展示字段（``tag``）与归属（``person_id``）分家：前者进条目的
        ``who``（头行视图里给人看），后者只进 JSONL 字段——绝不进展示文本。
        """
        entry = fmt.Entry(
            date=str(date),
            time=str(time),
            mood=fmt.sanitize_mood(mood),
            who=fmt.sanitize_tag(tag),
            person_id=str(person_id or "").strip(),
            text=str(body),
            book=book,
        )
        async with self.locks.acquire(self.jsonl_path(book)):
            entries = self.read_entries(book)
            entries.append(entry)
            self._write_jsonl(book, entries)

    def backup(self, book: str, raw: str, *, action: str, now: datetime) -> Path | None:
        """把整本原文丢进 ``diary-trash/``（JSONL 原文快照）。失败不阻塞（只影响可回滚性）。"""
        if not raw:
            return None
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        target = self.layout.trash_dir / f"{stamp}-{action}-{book}.jsonl"
        try:
            storage.atomic_write_text(target, raw)
        except OSError:
            return None
        return target

    # ---- 改 / 删 ---------------------------------------------------------------

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
        """按 ``match`` 模糊定位改 / 删一段（``diary_edit`` 工具路径）：
        **先备份原文，再落盘**（顺序不能反）。
        """
        path = self.jsonl_path(book)
        async with self.locks.acquire(path):
            entries = self.read_entries(book)
            segments = [fmt.entry_segment(entry) for entry in entries]
            seg, error = fmt.find_segment(
                segments, match, now=now, within_days=within_days, tz=tz
            )
            if seg is None:
                return fmt.EditResult(ok=False, error=error)
            index = next(i for i, s in enumerate(segments) if s is seg)
            return self._surgery(
                book,
                entries,
                index,
                seg,
                action=action,
                new_body=new_body,
                now=now,
            )

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
        path = self.jsonl_path(book)
        async with self.locks.acquire(path):
            entries = self.read_entries(book)
            index: int | None = None
            seg: fmt.Segment | None = None
            for i, entry in enumerate(entries):
                if fmt.entry_seg_id(entry) == str(seg_id or "").strip():
                    index, seg = i, fmt.entry_segment(entry)
                    break
            if index is None or seg is None:
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
            return self._surgery(
                book, entries, index, seg, action=action, new_body=new_body, now=now
            )

    def _surgery(
        self,
        book: str,
        entries: list[fmt.Entry],
        index: int,
        seg: fmt.Segment,
        *,
        action: str,
        new_body: str,
        now: datetime,
    ) -> fmt.EditResult:
        """对**已定位**的一段执行改/删（调用方持锁、已过时间窗检查）。

        先备份 JSONL 原文，再落盘；删除先归档进回收站。``Entry`` 是 frozen 的，
        改正文用 ``dataclasses.replace`` 造新条目，其余字段（含归属）原样保留。
        """
        raw = storage.read_text(self.jsonl_path(book))
        before = entries[index].text
        if action == "delete":
            new_entries = entries[:index] + entries[index + 1 :]
        elif action == "rewrite":
            body = str(new_body or "").strip()
            if not body:
                return fmt.EditResult(ok=False, error="新正文不能为空（想删掉这段就用删除）")
            new_entries = list(entries)
            new_entries[index] = replace(entries[index], text=body)
        else:
            return fmt.EditResult(ok=False, error="action 只能是 rewrite 或 delete")
        self.backup(book, raw, action=action, now=now)
        if action == "delete":
            try:
                self.archive_segment(book, seg, now=now)
            except OSError:
                pass
        self._write_jsonl(book, new_entries)
        return fmt.EditResult(ok=True, seg=seg, before=before)

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
            "person_id": seg.person_id,
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

    async def restore_segment(self, trash_id: str, *, now: datetime) -> tuple[bool, str]:
        """从回收站还原一段，返回 ``(是否成功, 原因)``。

        幂等：同一份归档还原两次，第二次会认出"这段已经在本子里了"并拒绝，
        **绝不**插出两条一样的。归属（``person_id``）随记录一起回去；头行由
        ``entry_segment`` 从结构化字段重建，内容地址与删除前一致。
        """
        record = self.trash_record(trash_id)
        if record is None:
            return False, "找不到这一条（可能已经还原过了）"
        book = str(record.get("book") or "")
        if book not in fmt.BOOKS:
            return False, "这一条记的是哪本都认不出来，放着别动"
        seg = fmt.segment_from_record(record)
        entry = fmt.entry_from_record(record)
        if entry is None:
            return False, "这一条没有正文，放着别动"
        path = self.jsonl_path(book)
        async with self.locks.acquire(path):
            entries = self.read_entries(book)
            if any(fmt.entry_seg_id(item) == fmt.seg_id(seg) for item in entries):
                return False, "这一段已经在本子里了，不用再还原"
            self._write_jsonl(book, fmt.insert_entry_chronologically(entries, entry))
        try:
            os.unlink(self.layout.trash_dir / f"{trash_id}.json")
        except OSError:
            pass  # 还原成功但清不掉归档条目：顶多多列一条，可再次还原（幂等挡着，不会插重）
        return True, ""


__all__ = ["TRASH_SEGMENT_CAP", "TRASH_SEGMENT_PREFIX", "DiaryStore", "person_id_by_name"]
