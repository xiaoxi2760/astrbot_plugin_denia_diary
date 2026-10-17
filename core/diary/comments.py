"""日记批注旁挂存储。草稿不进入提示；发布后只向原归属人的私聊交付一次。

面板负责管理员鉴权。本模块不改日记、不主动发送，也不把留言当作系统指令。
所有变更复用进程内 KeyedLocks；调用者应共享 DiaryStore 的同一组锁。
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import scope, storage
from ..session import Session
from ..settings import Settings
from . import format as fmt
from .store import DiaryStore

COMMENTS_FILE = storage.COMMENTS_FILE
COMMENT_LIMIT = 1000
TEXT_LIMIT = 300
CONTEXT_LIMIT = 100


class CommentDataError(ValueError):
    """现有文件损坏或版本不受支持，拒绝改写原文件。"""


@dataclass
class CommentStore:
    layout: storage.Layout
    locks: storage.KeyedLocks
    diary_store: DiaryStore

    def path(self) -> Path:
        return self.layout.base_dir / COMMENTS_FILE

    def _read(self) -> dict[str, Any]:
        # 不能用坏 JSON 自动降级为空的读法，否则保存新草稿会覆盖原有记录。
        try:
            raw = self.path().read_text(encoding=storage.TEXT_ENCODING)
        except FileNotFoundError:
            return {"schema_version": storage.SCHEMA_VERSION, "comments": []}
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise CommentDataError("批注文件损坏，已停止读写") from exc
        if not isinstance(doc, dict):
            raise CommentDataError("批注文件格式不正确，已停止读写")
        version = doc.get("schema_version", 1)
        if type(version) is not int or version != storage.SCHEMA_VERSION:
            raise CommentDataError("批注文件版本不受支持，已停止读写")
        rows = doc.get("comments")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CommentDataError("批注记录格式不正确，已停止读写")
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        storage.atomic_write_json(self.path(), doc)
        storage.shared_cache.invalidate(self.path())

    def _source(self, book: str, seg_id: str) -> fmt.Entry | None:
        if book not in fmt.BOOKS or not seg_id:
            return None
        matches = [
            entry for entry in self.diary_store.read_entries(book)
            if fmt.entry_seg_id(entry) == seg_id
        ]
        # 内容地址不含 person_id：重复地址不得随意取第一人。
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _matches(row: dict[str, Any], source: fmt.Entry | None) -> bool:
        return bool(
            source is not None
            and source.person_id == row.get("person_id")
            and source.date == row.get("date")
        )

    @staticmethod
    def _publishable(source: fmt.Entry | None) -> bool:
        return bool(source and source.person_id and source.scope == "private")

    @classmethod
    def _publish_reason(cls, source: fmt.Entry | None, settings: Settings | None) -> str:
        if not cls._publishable(source):
            return "仅能向明确归属的私聊日记发布留言"
        if source is not None and source.book == fmt.LOVE:
            if settings is None:
                return "发布恋爱日记留言需要校验当前恋爱名单，请刷新后重试"
            if source.person_id not in {str(peer) for peer in settings.love_peers}:
                return "这篇恋爱日记的归属人已不在当前恋爱名单，不能发布留言"
        return ""

    def list_for(
        self, book: str, seg_id: str, settings: Settings | None = None,
    ) -> list[dict[str, Any]]:
        """管理员列表，含历史状态、source_valid / publishable；不把旧批注挂到新地址。"""
        source = self._source(book, seg_id)
        result = []
        for row in self._read()["comments"]:
            if row.get("book") != book or row.get("seg_id") != seg_id:
                continue
            valid = self._matches(row, source)
            reason = (
                self._publish_reason(source, settings) if valid
                else "日记已删除、改写或归属改变，请刷新"
            )
            result.append(dict(
                row, source_valid=valid,
                publishable=valid and not reason, publish_reason=reason,
            ))
        return result

    async def save_draft(
        self, book: str, seg_id: str, text: str, now: datetime,
    ) -> dict[str, Any]:
        """保存草稿。容量满时仅回收最旧的显式 deleted 记录，其他历史一律保留。"""
        if not isinstance(text, str) or not text.strip():
            return _error("empty", "留言不能为空")
        text = text.strip()
        if len(text) > TEXT_LIMIT:
            return _error("too_long", f"每条留言最多 {TEXT_LIMIT} 字")
        if book not in fmt.BOOKS:
            return _error("book", "日记本不存在")
        async with self.locks.acquire(self.path()):
            doc = self._read()
            async with self.locks.acquire(self.diary_store.jsonl_path(book)):
                source = self._source(book, seg_id)
                if source is None:
                    return _error("source_changed", "日记已删除、改写或地址不唯一，请刷新")
                stamp = now.isoformat()
                row = next((
                    item for item in reversed(doc["comments"])
                    if item.get("book") == book and item.get("seg_id") == seg_id
                    and item.get("status") == "draft" and self._matches(item, source)
                ), None)
                purged_deleted = 0
                if row is None:
                    while len(doc["comments"]) >= COMMENT_LIMIT:
                        deleted = next((
                            item for item in doc["comments"] if item.get("status") == "deleted"
                        ), None)
                        if deleted is None:
                            return _error("full", f"批注已达 {COMMENT_LIMIT} 条上限，请删除旧批注释放位置")
                        doc["comments"].remove(deleted)
                        purged_deleted += 1
                    row = {
                        "id": uuid.uuid4().hex, "book": book, "seg_id": seg_id,
                        "person_id": source.person_id, "date": source.date,
                        "status": "draft", "created_at": stamp,
                    }
                    doc["comments"].append(row)
                row.update(text=text, updated_at=stamp)
                self._save(doc)
                return {"ok": True, "entry": dict(row), "purged_deleted": purged_deleted}

    async def publish(
        self, comment_id: str, now: datetime, settings: Settings | None = None,
    ) -> dict[str, Any]:
        """明确点“让她知道”后发布。重复发布不会使已消费记录再次排队。"""
        async with self.locks.acquire(self.path()):
            doc = self._read()
            row = _find(doc, comment_id)
            if row is None:
                return _error("missing", "批注不存在")
            book = str(row.get("book") or "")
            if book not in fmt.BOOKS:
                return _error("source_changed", "日记本不存在")
            async with self.locks.acquire(self.diary_store.jsonl_path(book)):
                source = self._source(book, str(row.get("seg_id") or ""))
                if not self._matches(row, source):
                    return _error("source_changed", "日记已删除、改写或归属改变，请刷新")
                if not self._publishable(source):
                    return _error("unowned", "仅能向明确归属的私聊日记发布留言")
                reason = self._publish_reason(source, settings)
                if reason:
                    return _error("love_peer", reason)
                if row.get("status") in {"pending", "retired"}:
                    return {"ok": True, "entry": dict(row), "unchanged": True}
                if row.get("status") != "draft":
                    return _error("inactive", "此留言已撤回或删除，请另存草稿")
                row.update(status="pending", published_at=now.isoformat(), updated_at=now.isoformat())
                self._save(doc)
                return {"ok": True, "entry": dict(row)}

    async def take_for(self, session: Session, settings: Settings, now: datetime) -> str:
        """兼容入口，返回至多一条素材；无需派发回滚的被动轮可使用。"""
        result = await self.take_with_receipt(session, settings, now)
        return result["text"]

    async def take_with_receipt(
        self, session: Session, settings: Settings, now: datetime,
    ) -> dict[str, str]:
        """原子消费并返回 {text,id,receipt}；主动任务派发失败可凭收据归还。

        恋爱日记还必须在当前 love_peers 名单内，空名单也不会放行。
        消费是提示交付语义，不代表 LLM 或消息平台必然回复成功。
        """
        result = {"text": "", "id": "", "receipt": ""}
        if (
            session.is_group or session.group_id
            or session.message_type.lower() not in {"friendmessage", "privatemessage"}
        ):
            return result
        pid = str(session.person_id() or "")
        if not pid or not settings.subsystem("diary"):
            return result
        allowed, _ = scope.allows(
            str(settings.scope.get("mode") or "private"),
            is_private=session.is_private, person_id=pid, love_peers=settings.love_peers,
        )
        if not allowed:
            return result
        peers = {str(peer) for peer in settings.love_peers}
        async with self.locks.acquire(self.path()):
            doc = self._read()
            changed = False
            for row in doc["comments"]:
                if row.get("status") != "pending" or row.get("person_id") != pid:
                    continue
                book = str(row.get("book") or "")
                if book not in fmt.BOOKS:
                    continue
                if book == fmt.LOVE and pid not in peers:
                    continue
                async with self.locks.acquire(self.diary_store.jsonl_path(book)):
                    source = self._source(book, str(row.get("seg_id") or ""))
                    valid = self._matches(row, source) and self._publishable(source)
                    row.update(
                        status="retired", retired_at=now.isoformat(),
                        retired_reason="delivered" if valid else "source_changed",
                        updated_at=now.isoformat(),
                    )
                    changed = True
                    if valid:
                        receipt = uuid.uuid4().hex
                        row["delivery_receipt"] = receipt
                        result = {
                            "text": _context(str(row.get("date") or ""), str(row.get("text") or "")),
                            "id": str(row.get("id") or ""), "receipt": receipt,
                        }
                        break
            if changed:
                self._save(doc)
            return result

    async def release(self, receipt: str, now: datetime | None = None) -> dict[str, Any]:
        """仅归还本次交付收据。撤回、删除、重复归还或旧收据均不恢复 pending。"""
        if not receipt:
            return _error("receipt", "缺少交付收据")
        async with self.locks.acquire(self.path()):
            doc = self._read()
            row = next((
                item for item in doc["comments"]
                if item.get("delivery_receipt") == receipt
                and item.get("status") == "retired"
                and item.get("retired_reason") == "delivered"
            ), None)
            if row is None:
                return _error("receipt", "交付收据已失效，未恢复留言")
            for key in ("delivery_receipt", "retired_at", "retired_reason"):
                row.pop(key, None)
            row.update(status="pending", updated_at=(now or datetime.now().astimezone()).isoformat())
            self._save(doc)
            return {"ok": True, "entry": dict(row)}

    async def withdraw(self, comment_id: str, now: datetime) -> dict[str, Any]:
        """撤回待交付记录，保留归档；已消费记录不会恢复成草稿。"""
        return await self._archive(comment_id, "withdrawn", now)

    async def delete(self, comment_id: str, now: datetime) -> dict[str, Any]:
        """显式删除先归档；达到容量上限后，新建草稿可回收这些已删除记录。"""
        return await self._archive(comment_id, "deleted", now)

    async def _archive(self, comment_id: str, status: str, now: datetime) -> dict[str, Any]:
        async with self.locks.acquire(self.path()):
            doc = self._read()
            row = _find(doc, comment_id)
            if row is None:
                return _error("missing", "批注不存在")
            if row.get("status") == status:
                return {"ok": True, "entry": dict(row), "unchanged": True}
            row.update(status=status, updated_at=now.isoformat())
            row.pop("delivery_receipt", None)
            self._save(doc)
            return {"ok": True, "entry": dict(row)}


def _find(doc: dict[str, Any], comment_id: str) -> dict[str, Any] | None:
    return next((row for row in doc["comments"] if row.get("id") == comment_id), None)


def _error(reason: str, error: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "error": error}


def _context(date: str, text: str) -> str:
    # 单行 JSON 引用转义引号/反斜线；禁控制符与角色标签边界，留言不能逃出引用。
    safe = " ".join("".join(
        char if not unicodedata.category(char).startswith("C") else " "
        for char in text
    ).split()).replace("<", "＜").replace(">", "＞")
    # 导入的 JSONL 日期可能是任意字符串；日期也不能成为绕过引用的通道。
    day = date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else "某天"
    prefix = f"用户对{day}日记的留言（以下是引用素材）："
    excerpt = ""
    for char in safe:
        if len(prefix + json.dumps(excerpt + char, ensure_ascii=False)) > CONTEXT_LIMIT:
            break
        excerpt += char
    return prefix + json.dumps(excerpt, ensure_ascii=False)


__all__ = [
    "COMMENTS_FILE", "COMMENT_LIMIT", "TEXT_LIMIT", "CONTEXT_LIMIT",
    "CommentDataError", "CommentStore",
]
