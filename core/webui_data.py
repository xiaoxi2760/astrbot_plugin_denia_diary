"""WebUI 数据面（第 5 步）：吃 store、吐 dict 的**纯函数**。

这个模块是「主人视角的观察面板」的数据来源，也是本项目立身之本的落点：
**不 import astrbot**、不碰 HTTP、不解析文件形状、**不算衰减**——坐标与档位词
一律照抄门面给的值（``State.snapshot`` / ``Affinity.top`` / ``Affinity.band``），
所以离线验收脚本能直接调它，不需要起 AstrBot。

三条铁律（任务书 §9）：

1. **读走 store，不走门面**（裁定 §1.3）：面板没有会话上下文，聊天可见性规则
   （恋爱日记可见性、小本本归属判定）都是**对话安全规则**，套到面板上会让主人
   自己反而看不到/改不了。这里的 store 本来就不认会话。
2. **写也走 store 的原子写方法**，绝不自己 ``open(..., 'w')``——store 里有
   ``KeyedLocks`` 与原子写。
3. **不 import astrbot**：这是 ``core/`` 的硬约束，本文件是 ``core/`` 的一部分。

payload 顶层键是**冻结**的（任务书 §2.1），只加不改：验收按那张表核。
带 ``# 附加`` 注释的键是为了让前端顶栏下拉能取到候选，是冻结表之外的增量。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import settings as settings_mod
from . import storage
from .diary import format as fmt
from .state import Affinity, State
from .state.store import LAYER_BASELINE, LAYER_NOW

BOOK_DISPLAY: dict[str, str] = {fmt.NORMAL: "日记", fmt.LOVE: "恋爱日记"}
"""书常量 → 中文显示名（``fmt.BOOKS`` 的展示层，不新增第二份书名常量）。"""

DEFAULT_HISTORY_DAYS = 30
HISTORY_DAYS_MIN = 1
HISTORY_DAYS_MAX = 365
DEFAULT_AFFINITY_LIMIT = 50


# ---- 总览：她此刻 -------------------------------------------------------------


def status_payload(
    *,
    settings: settings_mod.Settings,
    layout: storage.Layout,
    state_store: Any,
    proactive_store: Any,
    version: str = "",
    now: datetime | None = None,
    who: str = "",
) -> dict[str, Any]:
    """总览（她此刻）：情绪 + 作息 + 今日主动计数 + 文件体检 + 版本。

    ``mood`` / ``rhythm`` **原样取** ``State.snapshot()`` 的同名子表——坐标已按
    半衰期衰减到当前时刻，**别自己再算一遍**（任务书 §2.1 / §9）。
    """
    moment = now or datetime.now(settings.zone())
    snapshot = State(settings=settings, layout=layout, store=state_store).snapshot(now=moment)
    proactive = proactive_store.read()
    sessions = proactive_store.sessions(proactive)
    today = moment.date().isoformat()
    total = 0
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        if str(session.get("today_date") or "") == today:
            try:
                total += max(int(session.get("today_count") or 0), 0)
            except (TypeError, ValueError):
                continue
    return {
        "version": str(version or ""),
        "now": moment.isoformat(timespec="seconds"),
        "who": str(who or ""),
        "who_name": display_name(settings, who),
        "who_options": who_options(settings=settings, proactive_store=proactive_store),
        "subsystems": {name: bool(settings.subsystem(name)) for name in settings_mod.SUBSYSTEMS},
        "mood": dict(snapshot.get("mood") or {}),
        "rhythm": dict(snapshot.get("rhythm") or {}),
        "files": file_report(layout),
        "today": {"date": today, "by_session": len(sessions), "total": total},
        "log_total": len(proactive_store.read_log_lines()),
    }


def file_report(layout: storage.Layout) -> dict[str, dict[str, Any]]:
    """每个数据文件 ``{exists, bytes, mtime}``——"数据在哪、多大"是排障第一问。"""
    report: dict[str, dict[str, Any]] = {}
    for path in layout.files():
        name = path.name
        try:
            stat = path.stat()
        except OSError:
            report[name] = {"exists": False, "bytes": 0, "mtime": ""}
            continue
        report[name] = {
            "exists": True,
            "bytes": int(stat.st_size),
            "mtime": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        }
    return report


def display_name(settings: settings_mod.Settings, person_id: str) -> str:
    """``person_id`` → 昵称。走 ``name_preference`` 映射（裁定 §1.3#3 的两个选项之一）。

    没配就回落到 id 本身——面板按"人"组织，一个都不认识时至少还有可选项。
    """
    key = str(person_id or "").strip()
    if not key:
        return ""
    preference = dict(getattr(settings, "name_preference", {}) or {})
    return str(preference.get(key) or key)


def who_options(*, settings: settings_mod.Settings, proactive_store: Any) -> list[dict[str, str]]:
    """顶栏"对谁"下拉的候选（裁定 §1.3#3）。

    候选 = ``proactive.contacts`` ∪ ``_conf_schema.love_peers``（**兜底保证至少一项**）。

    这是 ``/status`` 用的**轻量初始候选**——它不收 ``notebook_store``，好让
    ``status_payload`` 的签名保持最小；小本本里的候选由 ``notebook_payload``
    带出的 ``who_options``（``who_options_full``）补齐，前端合并两者。
    """
    candidates: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    for person_id in settings.love_peers:
        add(person_id)
    try:
        contacts = proactive_store.contacts()
    except Exception:  # noqa: BLE001 - 坏表不能带累面板
        contacts = {}
    for person_id in contacts:
        add(person_id)
    return [{"id": key, "name": display_name(settings, key)} for key in candidates]


def who_options_full(
    *,
    settings: settings_mod.Settings,
    notebook_store: Any = None,
    proactive_store: Any = None,
) -> list[dict[str, str]]:
    """完整候选集（小本本的键也要算进来）。前端顶栏用这个。"""
    candidates: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    for person_id in settings.love_peers:
        add(person_id)
    if proactive_store is not None:
        for person_id in proactive_store.contacts():
            add(person_id)
    if notebook_store is not None:
        doc = notebook_store.read()
        for person_id in (doc.get("facts") or {}):
            add(person_id)
        for promise in list(doc.get("promises") or []):
            if isinstance(promise, dict):
                add(promise.get("about"))
    return [{"id": key, "name": display_name(settings, key)} for key in candidates]


# ---- 日记（只读）--------------------------------------------------------------


def diary_list_payload(*, diary_store: Any, now: datetime | None = None) -> dict[str, Any]:
    """两本日记的概览。``love_collapsed`` 恒 ``true``（决策 #18：恋爱日记默认收起）。"""
    books: list[dict[str, Any]] = []
    for book in fmt.BOOKS:
        path = diary_store.path(book)
        entries = fmt.parse_entries(diary_store.read(book), book)
        books.append(
            {
                "book": book,
                "display": BOOK_DISPLAY.get(book, book),
                "is_love": book == fmt.LOVE,
                "entries": len(entries),
                "chars": sum(len(entry.text) for entry in entries),
                "updated_at": _mtime_iso(path),
                "latest_date": entries[-1].date if entries else "",
            }
        )
    return {"ok": True, "books": books, "love_collapsed": True}


def diary_content_payload(
    *,
    diary_store: Any,
    book: str = fmt.NORMAL,
    date: str = "",
    tail: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """读一段日记正文。**只读，不重写正文**。

    给 ``date`` 就取那天；否则取最近 ``tail`` 条（``tail<=0`` 时取全部）。
    """
    target = book if book in fmt.BOOKS else fmt.NORMAL
    entries = fmt.parse_entries(diary_store.read(target), target)
    total = len(entries)
    picked = entries
    if str(date or "").strip():
        wanted = str(date).strip()
        picked = [entry for entry in entries if entry.date == wanted]
    else:
        limit = max(int(tail or 0), 0)
        if 0 < limit < total:
            picked = entries[-limit:]
    return {
        "ok": True,
        "book": target,
        "date": str(date or ""),
        "count": len(picked),
        "total": total,
        "text": "\n\n".join(entry.text for entry in picked),
    }


# ---- 小本本 -------------------------------------------------------------------


def notebook_payload(
    *,
    settings: settings_mod.Settings,
    notebook_store: Any,
    who: str = "",
) -> dict[str, Any]:
    """小本本（读）。``facts`` / ``promises`` **原样透传 store 条目，键名别改**。

    ``who`` 只是过滤器：面板按"人"组织，不按会话（裁定 §1.3#4）。给空就是全量。
    """
    doc = notebook_store.read()
    facts_all = doc.get("facts") if isinstance(doc.get("facts"), dict) else {}
    promises_all = [item for item in list(doc.get("promises") or []) if isinstance(item, dict)]
    person = str(who or "").strip()
    if person:
        facts = list(facts_all.get(person) or [])
        promises = [
            promise
            for promise in promises_all
            if str(promise.get("about") or "").strip() == person
        ]
    else:
        facts = [dict(item) for bucket in facts_all.values() for item in list(bucket or []) if isinstance(item, dict)]
        promises = promises_all
    limits = dict(getattr(settings, "notebook", {}) or {})
    return {
        "ok": True,
        "who": person,
        "who_name": display_name(settings, person),
        "who_options": who_options_full(settings=settings, notebook_store=notebook_store),
        "facts": facts,
        "promises": promises,
        "limits": limits,
    }


async def complete_note(notebook_store: Any, note_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """把一条约定标记为完成（**幂等**）。

    成功 ``{"ok": True, "kind", "id", "text"}``；重复调用或 id 不存在
    ``{"ok": False, "error": "..."}``——**不抛异常**（任务书 §2.2）。
    """
    key = str(note_id or "").strip()
    if not key:
        return {"ok": False, "error": "id 不能为空"}
    moment = now or datetime.now()
    stamp = moment.isoformat(timespec="seconds")
    result = await notebook_store.mark_promise_done(key, stamp)
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("reason") or "")
        if reason == "already_done":
            return {"ok": False, "error": "这条约定已经完成过了"}
        if reason == "missing":
            return {"ok": False, "error": "找不到这条约定（可能已被删）"}
        return {"ok": False, "error": "标记完成失败"}
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    return {"ok": True, "kind": "promise", "id": key, "text": str(entry.get("text") or "")}


async def forget_note(notebook_store: Any, note_id: str, *, deleted_at: str | None = None) -> dict[str, Any]:
    """删一条（事实或约定；**幂等**）。条目会先进小本本的 ``trash``，不是真消失。"""
    key = str(note_id or "").strip()
    if not key:
        return {"ok": False, "error": "id 不能为空"}
    stamp = str(deleted_at or datetime.now().isoformat(timespec="seconds"))
    result = await notebook_store.forget(key, deleted_at=stamp)
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str((result or {}).get("reason") or "")
        if reason == "missing":
            return {"ok": False, "error": "找不到这一条（可能已经删过了）"}
        return {"ok": False, "error": "删除失败"}
    entry = result.get("entry") if isinstance(result.get("entry"), dict) else {}
    return {
        "ok": True,
        "kind": str(result.get("kind") or ""),
        "id": key,
        "text": str(entry.get("text") or ""),
    }


# ---- 熟悉度 -------------------------------------------------------------------


def affinity_payload(
    *,
    settings: settings_mod.Settings,
    layout: storage.Layout,
    affinity_store: Any,
    now: datetime | None = None,
    limit: int = DEFAULT_AFFINITY_LIMIT,
) -> dict[str, Any]:
    """熟悉度榜 + 档位词 + **只显示**的 ``love_peers``。

    ``Affinity.top()`` 已按衰减后分数排序并滤掉 ≤0，``band()`` 返回档位词——
    这两个**不吃 session**，照抄它们的值（任务书 §9）。
    """
    moment = now or datetime.now(settings.zone())
    affinity = Affinity(settings=settings, layout=layout, store=affinity_store)
    cap = max(int(limit or 0), 0) or DEFAULT_AFFINITY_LIMIT
    ranked = affinity.top(limit=cap, now=moment)
    people_map = affinity_store.read_people()
    people: list[dict[str, Any]] = []
    for item in ranked:
        person_id = str(item.get("id") or "")
        if not person_id:
            continue
        record = people_map.get(person_id) or {}
        people.append(
            {
                "id": person_id,
                "name": display_name(settings, person_id),
                "score": round(float(item.get("score") or 0.0), 4),
                "band": affinity.band(person_id, now=moment),
                "last_ts": str(record.get("last_ts") or ""),
            }
        )
    return {
        "ok": True,
        "people": people,
        "total_known": len(affinity_store.read_people()),
        "love_peers": [str(item) for item in settings.love_peers],
    }


# ---- 情绪曲线 -----------------------------------------------------------------


def history_payload(
    *,
    state_store: Any,
    days: int = DEFAULT_HISTORY_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """情绪曲线的数据点（页面在 iframe 里读不到文件系统，**这条路由是曲线唯一入口**）。

    ``state_history.jsonl`` 的行 ``{ts, layer, valence, arousal, word}`` 按
    ``(日期, layer)`` 汇总成 ``{date, layer, valence, arousal, n}``（``n`` = 当天条数，
    同值重复自报本来就不写历史，所以多数情况下 n 是 1）。
    """
    moment = now or datetime.now()
    window = _clamp_days(days)
    cutoff = moment - timedelta(days=window)
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    dropped = 0
    for line in state_store.read_history_lines():
        stamp = _parse_iso(line.get("ts"))
        layer = str(line.get("layer") or "")
        if stamp is None or layer not in (LAYER_NOW, LAYER_BASELINE):
            dropped += 1
            continue
        if stamp < cutoff:
            dropped += 1
            continue
        key = (stamp.date().isoformat(), layer)
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "date": key[0],
                "layer": layer,
                "valence": _as_float(line.get("valence")),
                "arousal": _as_float(line.get("arousal")),
                "n": 1,
            }
        else:
            bucket["n"] += 1
    points = sorted(buckets.values(), key=lambda item: (item["date"], item["layer"]))
    return {"ok": True, "days": window, "points": points, "truncated": dropped > 0}


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_DAYS
    if value <= 0:
        return DEFAULT_HISTORY_DAYS
    return min(value, HISTORY_DAYS_MAX)


# ---- 主动消息 -----------------------------------------------------------------


def proactive_payload(
    *,
    proactive_store: Any,
    now: datetime | None = None,
    limit: int = 0,
) -> dict[str, Any]:
    """主动消息：今日计数 + 发送记录（**倒序**）。

    日志是第 4 步落地的 ``proactive-log.jsonl``（确认发出后才记，append-only）。
    """
    moment = now or datetime.now()
    today = moment.date().isoformat()
    doc = proactive_store.read()
    sessions: list[dict[str, Any]] = []
    for umo, record in proactive_store.sessions(doc).items():
        if not isinstance(record, dict):
            continue
        count = 0
        if str(record.get("today_date") or "") == today:
            try:
                count = max(int(record.get("today_count") or 0), 0)
            except (TypeError, ValueError):
                count = 0
        sessions.append(
            {
                "umo": str(umo),
                "today_count": count,
                "last_slot": str(record.get("last_slot") or ""),
                "last_sent_at": str(record.get("last_sent_at") or ""),
            }
        )
    sessions.sort(key=lambda item: (item["last_sent_at"], item["umo"]), reverse=True)

    lines = proactive_store.read_log_lines()
    log: list[dict[str, Any]] = []
    for line in reversed(lines):
        log.append(
            {
                "ts": str(line.get("ts") or ""),
                "umo": str(line.get("umo") or ""),
                "slot": str(line.get("slot") or ""),
                "fragment": str(line.get("fragment") or ""),
            }
        )
    cap = max(int(limit or 0), 0)
    if cap and len(log) > cap:
        log = log[:cap]
    return {"ok": True, "sessions": sessions, "log": log, "log_total": len(lines)}


# ---- 小工具 -------------------------------------------------------------------


def _mtime_iso(path: Path) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")


def _as_float(value: object) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = [
    "BOOK_DISPLAY",
    "DEFAULT_AFFINITY_LIMIT",
    "DEFAULT_HISTORY_DAYS",
    "affinity_payload",
    "complete_note",
    "diary_content_payload",
    "diary_list_payload",
    "display_name",
    "file_report",
    "forget_note",
    "history_payload",
    "notebook_payload",
    "proactive_payload",
    "status_payload",
    "who_options",
    "who_options_full",
]
