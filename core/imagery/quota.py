"""每日生图次数记账（任务 2 第 5 条）：**请求发出即计费**。

语义与"模型请求开始后即可能计费"对齐：

- ``spend`` 在 HTTP 请求**发出之前**落盘 +1；超时、供应商报错、返回坏图都**不返还**
  ——云端的这笔钱已经花了，返还等于允许"超时无限重试白嫖"；
- 只有**请求根本没发出去**的情况不计费：功能关闭、缓存命中、并发拒绝、上限拒绝、
  配置不完整——这些在 ``api.py`` 里于 ``spend`` 之前就返回了；
- 跨天自动清零（按插件时区的日期），当天事件留痕（时间 + 结果短语，**不含提示词、
  不含密钥**），供面板与排障核对"今日到底打了几枪"；
- 记账落盘失败（``OSError``）原样上抛：门面把它当"拒绝发请求"处理——宁可少画，
  不能绕过费用上限。

只依赖标准库；不 import astrbot。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import storage

MAX_EVENTS = 20
"""当天事件留痕上限（新的顶掉旧的）：足够审计，不至于无限膨胀。"""


class QuotaDataError(ValueError):
    """配额账本存在但不可安全使用；必须拒绝生成且不得覆盖原文件。"""


class DailyQuota:
    """``imagery-quota.json`` 的读写。路径与锁由调用方注入。"""

    def __init__(self, path: Path, locks: Any = None) -> None:
        self.path = Path(path)
        self.locks = locks

    def _lock(self) -> asyncio.Lock:
        if self.locks is None:
            self.locks = storage.KeyedLocks()
        return self.locks.lock_for(self.path)

    def _read(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"date": "", "used": 0, "events": []}
        except UnicodeDecodeError as error:
            raise QuotaDataError("生图次数账本损坏，已停止生成；请检查原文件。") from error
        except OSError:
            raise
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise QuotaDataError("生图次数账本损坏，已停止生成；请检查原文件。") from error
        if not isinstance(doc, dict):
            raise QuotaDataError("生图次数账本格式损坏，已停止生成；请检查原文件。")
        date = doc.get("date")
        try:
            valid_date = isinstance(date, str) and datetime.fromisoformat(date).date().isoformat() == date
        except ValueError:
            valid_date = False
        used = doc.get("used")
        events = doc.get("events")
        if not valid_date or isinstance(used, bool) or not isinstance(used, int) or used < 0:
            raise QuotaDataError("生图次数账本中的日期或用量无效，已停止生成；请检查原文件。")
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise QuotaDataError("生图次数账本中的事件记录无效，已停止生成；请检查原文件。")
        return {
            "date": date,
            "used": used,
            "events": [dict(event) for event in events],
        }

    def _today(self, now: datetime) -> str:
        return now.date().isoformat()

    def state(self, now: datetime) -> dict[str, Any]:
        """当前记账快照：``{"date", "used", "events"}``。跨天时内存里视为 0（不落盘）。"""
        today = self._today(now)
        try:
            doc = self._read()
        except QuotaDataError as error:
            return {"date": today, "used": None, "events": [], "error": str(error)}
        except OSError as error:
            return {"date": today, "used": None, "events": [], "error": f"无法读取生图次数账本：{error}"}
        if doc["date"] != today:
            return {"date": today, "used": 0, "events": []}
        return doc

    async def spend(self, *, result: str, detail: str, now: datetime) -> dict[str, Any]:
        """发请求前记一笔（+1）。落盘失败抛 ``OSError``，由调用方拒绝发请求。"""
        async with self._lock():
            doc = self._read()
            today = self._today(now)
            if doc["date"] > today:
                raise QuotaDataError("生图请求时间早于当前账本日期，已停止生成以保护新日期用量。")
            if doc["date"] != today:
                doc = {"date": today, "used": 0, "events": []}
            doc["used"] = int(doc["used"]) + 1
            doc["events"] = ([
                {"ts": now.isoformat(timespec="seconds"),
                 "result": str(result)[:24],
                 "detail": str(detail)[:60]},
                *doc["events"],
            ])[:MAX_EVENTS]
            storage.atomic_write_json(self.path, doc)
            return doc


__all__ = ["MAX_EVENTS", "DailyQuota"]
