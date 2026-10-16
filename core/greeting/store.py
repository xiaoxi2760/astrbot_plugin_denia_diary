"""早晚安记录的存取层（``greetings.json``）。

形状（``schema_version`` 由 ``atomic_write_json`` 自动补；键名只增不改）::

    {"schema_version": 1,
     "people": {"<person_id>": {"events": [
         {"date": "YYYY-MM-DD",   ← 归属日（凌晨 0-4 点的晚安归前一天，见 api.NIGHT_DAY_BOUNDARY）
          "kind": "morning|night",
          "ts": "ISO8601 带时区"}]}}}

- **事件流，不存派生值**：连续天数、睡眠时长都从 events 现算（派生值读取原则），
  改算法不用迁移数据；
- 每人封顶 ``KEEP_EVENTS`` 条、整体只留最近 ``KEEP_DAYS`` 天（裁剪发生在写入时）；
- 写穿不做合并写：问候一天至多两条，频率天生就低，与 proactive.json 同款选择。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .. import storage

KEEP_EVENTS = 120
"""每人保留的事件条数上限（约两个月 × 早晚各一条；裁剪发生在写入时）。"""

KEEP_DAYS = 90
"""事件保留天数：按行内 ``date`` 判，超期的行在下次写入时顺手丢掉。"""


@dataclass
class GreetingStore:
    """记录门面。布局与锁由调用方注入（内核不认平台、不认全局单例）。"""

    layout: storage.Layout
    locks: storage.KeyedLocks

    def read(self) -> dict[str, Any]:
        """读整表（mtime 缓存；坏 JSON / 非对象一律降级为空表，不抛错）。"""
        doc = storage.read_json(self.layout.greeting, default={})
        return doc if isinstance(doc, dict) else {}

    def people(self, doc: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """``people`` 子表（缺键 / 形状不对一律当空表）。"""
        doc = self.read() if doc is None else doc
        people = doc.get("people")
        return people if isinstance(people, dict) else {}

    async def update(
        self, update: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any] | None:
        """锁内读-改-写。``update`` 返回 ``None`` 表示"这次不用写"。

        与 proactive 同款：问候事件一天至多两条，写穿没有写放大问题；锁保证
        同一人的两轮并发消息不会互相覆盖。
        """
        async with self.locks.acquire(self.layout.greeting):
            doc = self.read()
            result = update(doc)
            if result is None:
                return None
            storage.atomic_write_json(self.layout.greeting, result)
            storage.shared_cache.invalidate(self.layout.greeting)
            return result
