"""小本本的文件层：``notebook.json`` 的读写、每类上限、每文件一把锁。

上限检查放在锁内（``add_fact`` / ``add_promise``），"查空位 + 追加"才是原子的，
并发加满不会超限；只读方法不加锁（mtime 缓存读 + 原子写保证读不到半截文件）。
删除不直接消失：条目移进同文件的 ``trash``（防她幻觉误删，WebUI 可查）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import storage

TRASH_KEEP = 50
"""回收站容量：超过就丢最旧（先进先出）。"""


@dataclass
class NotebookStore:
    """``notebook.json`` 的位置与变更。配置、布局、锁由调用方注入。"""

    layout: storage.Layout
    locks: storage.KeyedLocks

    def path(self) -> Path:
        return self.layout.notebook

    def read(self) -> dict[str, Any]:
        """读整本（走 mtime 缓存）。缺键补齐：调用方拿到的一定有三类键。"""
        doc = storage.read_json(
            self.path(),
            default={"facts": {}, "promises": [], "trash": []},
        )
        doc.setdefault("facts", {})
        doc.setdefault("promises", [])
        doc.setdefault("trash", [])
        return doc

    def _save(self, doc: dict[str, Any]) -> None:
        storage.atomic_write_json(self.path(), doc)
        storage.shared_cache.invalidate(self.path())

    async def add_fact(self, peer_id: str, entry: dict[str, Any], *, limit: int) -> dict:
        """给某人加一条事实。满了返回 ``{"ok": False, "reason": "full"}``，不报错、不写坏文件。"""
        async with self.locks.acquire(self.path()):
            doc = self.read()
            facts = dict(doc.get("facts") or {})
            bucket = list(facts.get(peer_id) or [])
            if len(bucket) >= max(int(limit), 0):
                return {"ok": False, "reason": "full"}
            bucket.append(entry)
            facts[peer_id] = bucket
            doc["facts"] = facts
            self._save(doc)
            return {"ok": True, "count": len(bucket)}

    async def add_promise(self, entry: dict[str, Any], *, limit: int) -> dict:
        """加一条约定。上限按该 about 的**未完成**条数计（完成即腾坑）。"""
        async with self.locks.acquire(self.path()):
            doc = self.read()
            promises = list(doc.get("promises") or [])
            about = str(entry.get("about") or "")
            open_count = sum(
                1
                for item in promises
                if str(item.get("about") or "") == about and not item.get("done_at")
            )
            if open_count >= max(int(limit), 0):
                return {"ok": False, "reason": "full"}
            promises.append(entry)
            doc["promises"] = promises
            self._save(doc)
            return {"ok": True, "count": open_count + 1}

    async def mark_promise_done(self, note_id: str, done_at: str) -> dict:
        """把约定标记为完成（写 ``done_at``）。条目留在原处：完成的约定不占上限坑位。"""
        async with self.locks.acquire(self.path()):
            doc = self.read()
            for promise in list(doc.get("promises") or []):
                if str(promise.get("id") or "") == note_id:
                    if promise.get("done_at"):
                        return {"ok": False, "reason": "already_done"}
                    promise["done_at"] = done_at
                    promise["updated_at"] = done_at
                    self._save(doc)
                    return {"ok": True, "entry": promise}
            return {"ok": False, "reason": "missing"}

    async def forget(self, note_id: str, *, deleted_at: str) -> dict:
        """删一条（事实或约定）：**先移进 ``trash`` 再落盘**，一次写完成。"""
        async with self.locks.acquire(self.path()):
            doc = self.read()
            promises = list(doc.get("promises") or [])
            for index, promise in enumerate(promises):
                if str(promise.get("id") or "") == note_id:
                    removed = promises.pop(index)
                    doc["promises"] = promises
                    self._push_trash(doc, kind="promise", entry=removed, deleted_at=deleted_at)
                    self._save(doc)
                    return {"ok": True, "kind": "promise", "entry": removed}
            facts = dict(doc.get("facts") or {})
            for peer_id, bucket in list(facts.items()):
                entries = list(bucket or [])
                for index, fact in enumerate(entries):
                    if str(fact.get("id") or "") == note_id:
                        removed = entries.pop(index)
                        if entries:
                            facts[peer_id] = entries
                        else:
                            facts.pop(peer_id, None)  # 空桶不攒空键
                        doc["facts"] = facts
                        self._push_trash(doc, kind="fact", entry=removed, deleted_at=deleted_at)
                        self._save(doc)
                        return {"ok": True, "kind": "fact", "entry": removed}
            return {"ok": False, "reason": "missing"}

    @staticmethod
    def _push_trash(
        doc: dict[str, Any], *, kind: str, entry: dict[str, Any], deleted_at: str
    ) -> None:
        trash = list(doc.get("trash") or [])
        trash.append({"kind": kind, "deleted_at": deleted_at, "entry": entry})
        doc["trash"] = trash[-TRASH_KEEP:]


__all__ = ["TRASH_KEEP", "NotebookStore"]
