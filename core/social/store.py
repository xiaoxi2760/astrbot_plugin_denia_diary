"""群社交状态表（``social.json``）的存取层。

形状（键一律用 **umo** 做群索引、**真实 id** 做参与者索引；读者不认的键一律忽略）::

    {"schema_version": 1,
     "groups": {"<umo>": {"umo", "platform", "group_id", "name",      ← 主人登记
                          "enabled": false, "registered_at", "updated_at"}},
     "participants": {"<群umo>": {"<pid>": {"name", "is_bot": false,   ← 主人登记
                                            "allow_interaction": false,
                                            "registered_at", "updated_at"}}},
     "seen": {"<umo>": {"platform", "group_id", "name", "last_seen_at"}},   ← 观察到的群（待登记）
     "topics": {"<umo>": [{"ts", "speaker_id", "speaker_name", "text", "hash"}]},
     "chats": {"<umo>": {"active": {...} | None, "cooldown_until", "recent_hashes": [...]}},
     "decisions": [{"ts", "umo", "action", "reason", "slot", "fragment", "round"}]}

**每一段都有界**（群社交任务：不无限攒历史）：

- ``groups`` / ``participants``：主人显式登记，写入侧按 ``max_groups`` 截断；
- ``seen``：观察到的群，只留最近 ``seen_max`` 个（给主人"一键登记"用）；
- ``topics``：每群最近 ``topic_max_items`` 条、单条 ``topic_max_chars`` 字、
  读取侧再按 TTL 过滤（过期即视为没有，写入时顺手清）；
- ``recent_hashes``：每群最多 ``HASH_KEEP`` 条（同消息去重的最小集合）；
- ``decisions``：最近 ``DECISIONS_KEEP`` 条（面板"最近为何发/为何没发"）。

写穿（与 ``proactive.json`` 同款）：每次变更原子落盘；消息观察频率被
"只处理已登记且启用的群" 天然限住，不做合并写。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .. import storage

DECISIONS_KEEP = 50
"""短聊/候选决策记录的保留条数（面板展示用，超出整表重写）。"""

HASH_KEEP = 64
"""每群保留的最近消息指纹数：同消息去重的窗口。64 条足够盖住刷屏重放，
又不随时间无限增长（旧指纹被真实新消息自然挤出）。"""


@dataclass
class SocialStore:
    """群社交状态门面。布局与锁都由调用方注入（内核不认平台、不认全局单例）。"""

    layout: storage.Layout
    locks: Any = field(default=None)

    def __post_init__(self) -> None:
        if self.locks is None:
            self.locks = storage.KeyedLocks()

    def read(self) -> dict[str, Any]:
        """读整表（mtime 缓存；坏 JSON / 非对象一律降级为空表，不抛错）。"""
        doc = storage.read_json(self.layout.social, default={})
        return doc if isinstance(doc, dict) else {}

    def section(self, doc: dict[str, Any], key: str) -> dict[str, Any]:
        """某个子表（缺键 / 形状不对一律当空表）。"""
        value = doc.get(key)
        return value if isinstance(value, dict) else {}

    async def update(
        self, update: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> dict[str, Any] | None:
        """锁内读-改-写（与 ``ProactiveStore.update`` 同款）。``update`` 返回
        ``None`` 表示"这次不用写"（省一次落盘）。

        并发事件（重放 / 乱序 / 两条消息同时到）在锁上天然串行：后进来的读到
        先进来的结果，轮数与指纹不会互相覆盖。
        """
        async with self.locks.acquire(self.layout.social):
            doc = self.read()
            result = update(doc)
            if result is None:
                return None
            storage.atomic_write_json(self.layout.social, result)
            storage.shared_cache.invalidate(self.layout.social)
            return result

    def truncate_doc(self, doc: dict[str, Any], *, seen_max: int) -> dict[str, Any]:
        """按上限裁剪"最近观察"与决策记录（登记表超限在调用侧拒绝，这里只兜底历史段）。"""
        seen = self.section(doc, "seen")
        if len(seen) > seen_max:
            keep = sorted(
                seen.items(),
                key=lambda item: str((item[1] or {}).get("last_seen_at") or ""),
                reverse=True,
            )[:seen_max]
            doc["seen"] = dict(keep)
        decisions = doc.get("decisions")
        if isinstance(decisions, list) and len(decisions) > DECISIONS_KEEP:
            doc["decisions"] = list(decisions[-DECISIONS_KEEP:])
        return doc


__all__ = ["DECISIONS_KEEP", "HASH_KEEP", "SocialStore"]
