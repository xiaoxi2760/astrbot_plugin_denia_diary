"""主动消息状态表（``proactive.json``）与发送记录（``proactive-log.jsonl``）的存取层。

**状态表写穿**：决策一 tick 至多一次（默认 15 分钟）、确认发送更低频，没有写放大
问题——不做合并写，每次变更直接原子落盘，``terminate`` 无内存态要收尾。与熟悉度的
1.5 秒合并写是两种刻意不同的选择：那边每次互动都写才需要合并，这边频率本身就低。

形状（``sessions[*].last_sent_at`` 是第 3 步 ``trace_line`` 的读取面，**只许加键
不许改名**；``contacts`` / ``slots_today`` / 暗号两键都是本步扩展，读者不认的一律忽略）::

    {"schema_version": 1,
     "sessions": {"<umo>": {"last_sent_at": "ISO8601 带时区",     ← 冻结键
                            "today_date": "YYYY-MM-DD",
                            "today_count": 0,
                            "last_slot": "greeting",
                            "slots_today": {"YYYY-MM-DD": ["greeting"]},
                            "signal_last_at": "ISO8601 带时区",
                            "signal_date": "YYYY-MM-DD"}},
     "contacts": {"<person_id>": {"umo": "...", "kind": "private",
                                  "name": "昵称（第 8 步加键：互动时从 sender_name 落盘，
                                           空名字不写也不擦已有值；不认这个键的读者忽略）"}}}

- 键一律用 **umo**（``trace_line`` 按 umo 优先、session_id 兜底查）；
- ``last_sent_at`` 只在**真的发出去那一刻**写（两段式第二步，确认点在钩子里）；
- ``today_count`` 在**决策时**就扣（两段式第一步）——唤醒失败就是配额白吃一次，
  换来的是轨迹永远不会替她说"她找过你了"。

**发送记录**（``proactive-log.jsonl``，验收 P.S. #11）：一行一个 JSON（``ts`` /
``umo`` / ``slot`` / ``fragment``），**确认发出后**才追加（决策不算），append-only，
保留最近 200 行或 90 天——裁剪时整文件重写一次（唯一重写时机）。它是 WebUI 三期
「主动消息记录」的唯一数据源（决策 #19）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .. import storage

LOG_KEEP_LINES = 200
"""发送记录的保留行数上限（裁剪时整文件重写）。"""

LOG_KEEP_DAYS = 90
"""发送记录的保留天数（按行内 ``ts`` 判，超期整文件重写）。"""


@dataclass
class ProactiveStore:
    """状态表门面。布局与锁都由调用方注入（内核不认平台、不认全局单例）。"""

    layout: storage.Layout
    locks: storage.KeyedLocks

    def read(self) -> dict[str, Any]:
        """读整表（mtime 缓存；坏 JSON / 非对象一律降级为空表，不抛错）。"""
        doc = storage.read_json(self.layout.proactive, default={})
        return doc if isinstance(doc, dict) else {}

    def sessions(self, doc: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """``sessions`` 子表（缺键 / 形状不对一律当空表）。"""
        doc = self.read() if doc is None else doc
        sessions = doc.get("sessions")
        return sessions if isinstance(sessions, dict) else {}

    def contacts(self, doc: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """``contacts`` 子表（person_id → 最后互动的会话；缺键当空表）。"""
        doc = self.read() if doc is None else doc
        contacts = doc.get("contacts")
        return contacts if isinstance(contacts, dict) else {}

    async def update(
        self, update: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any] | None:
        """锁内读-改-写。``update`` 返回 ``None`` 表示"这次不用写"（省一次落盘）。

        闭包拿到的是**最新读出的整表**，直接改完返回即可；锁保证同进程内
        决策与确认点不会互相覆盖。
        """
        async with self.locks.acquire(self.layout.proactive):
            doc = self.read()
            result = update(doc)
            if result is None:
                return None
            storage.atomic_write_json(self.layout.proactive, result)
            storage.shared_cache.invalidate(self.layout.proactive)
            return result

    # ---- 发送记录（proactive-log.jsonl） ---------------------------------------

    def log_path(self):
        return self.layout.proactive_log

    def read_log_lines(self) -> list[dict[str, Any]]:
        """读发送记录（坏行 / 缺 ``ts`` / ``ts`` 解析不了的行一律丢弃，不抛错）。"""
        path = self.log_path()
        if not path.exists():
            return []
        lines: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict) and _log_ts(parsed.get("ts")) is not None:
                lines.append(parsed)
        return lines

    async def append_log(self, lines: list[dict[str, Any]], *, now: datetime) -> None:
        """确认发出后追加记录；超过 200 行或有 90 天前的旧行时整文件重写一次。

        正常路径是纯追加（单行小写入 + fsync）；裁剪路径走 ``atomic_write_text``。
        """
        payload = [dict(line) for line in lines if isinstance(line, dict)]
        if not payload:
            return
        async with self.locks.acquire(self.log_path()):
            existing = self.read_log_lines()
            cutoff = now - timedelta(days=LOG_KEEP_DAYS)
            keep = [line for line in existing if (_log_ts(line.get("ts")) or now) >= cutoff]
            merged = keep + payload
            if len(keep) < len(existing) or len(merged) > LOG_KEEP_LINES:
                body = "".join(
                    json.dumps(line, ensure_ascii=False) + "\n"
                    for line in merged[-LOG_KEEP_LINES:]
                )
                storage.atomic_write_text(self.log_path(), body)
                storage.shared_cache.invalidate(self.log_path())
                return
            with open(self.log_path(), "a", encoding="utf-8", newline="") as handle:
                for line in payload:
                    handle.write(json.dumps(line, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            storage.shared_cache.invalidate(self.log_path())


def _log_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
