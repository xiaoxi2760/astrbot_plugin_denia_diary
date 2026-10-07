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
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import settings as settings_mod
from . import storage
from .diary import format as fmt
from .diary.store import TRASH_SEGMENT_CAP
from .state import Affinity, State
from .state.store import LAYER_BASELINE, LAYER_NOW

BOOK_DISPLAY: dict[str, str] = {fmt.NORMAL: "日记", fmt.LOVE: "恋爱日记"}
"""书常量 → 中文显示名（``fmt.BOOKS`` 的展示层，不新增第二份书名常量）。"""

DEFAULT_HISTORY_DAYS = 30
HISTORY_DAYS_MIN = 1
HISTORY_DAYS_MAX = 365
DEFAULT_AFFINITY_LIMIT = 50
MAX_CALENDAR_DAYS = 730
"""日历清单的回传上限（约两年）：一本日记写了五年也不能回几十万条，只保留最近的。"""


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
    try:
        contacts = proactive_store.contacts()
    except Exception:  # noqa: BLE001 - 坏表不能带累面板
        contacts = {}
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
        "who_name": display_name(settings, who, contacts=contacts),
        "who_options": who_options(settings=settings, proactive_store=proactive_store),
        "scope_mode": _scope_mode(settings),
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


def raw_name(
    settings: settings_mod.Settings,
    person_id: str,
    contacts: Mapping[str, Any] | None = None,
) -> str:
    """昵称本体（不带数字）：``name_preference`` → ``contacts[person]["name"]`` → 空。

    与 ``Session.label()`` 的既有序对齐（偏好 → 事件昵称 → id），面板与注入里
    不会出现两种叫法。``contacts`` 是 ``proactive.json`` 的联系人子表。
    """
    key = str(person_id or "").strip()
    if not key:
        return ""
    preference = dict(getattr(settings, "name_preference", {}) or {})
    name = str(preference.get(key) or "").strip()
    if not name and isinstance(contacts, Mapping):
        entry = contacts.get(key)
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or "").strip()
    return name


def display_name(
    settings: settings_mod.Settings,
    person_id: str,
    contacts: Mapping[str, Any] | None = None,
) -> str:
    """「对谁」的显示串（第 8 步）：**名字(数字)**，让下拉里不再是裸 id。

    - 名字来源：``name_preference[person]`` → ``contacts[person]["name"]`` → 空（同 ``raw_name``）；
    - 显示串：有名字且名字 ≠ id → ``名字(id)``；否则就是 id 本身——
      **绝不出现** ``1411638634(1411638634)`` 这种重复；
    - **不传 ``contacts`` 退回旧行为**（只认 ``name_preference``，不加数字后缀）——
      既有调用（熟悉度榜等没有联系人表的视图）一个都不破坏。
    """
    key = str(person_id or "").strip()
    if not key:
        return ""
    if contacts is None:
        preference = dict(getattr(settings, "name_preference", {}) or {})
        return str(preference.get(key) or key)
    name = raw_name(settings, key, contacts)
    if name and name != key:
        return f"{name}({key})"
    return key


def _scope_mode(settings: settings_mod.Settings) -> str:
    """当前启用范围档位（第 7 步的 ``scope.mode``，给前端显示"当前启用范围：只主人"）。"""
    return str(dict(settings.scope).get("mode") or "")


def _out_of_scope(
    mode: str, person_id: str, peers: set[str]
) -> bool:
    """当前档下她**不会**在这个人的会话里工作吗——只做标注，**一个候选都不删**
    （面板是主人视角、永远可用，第 7 步裁定）。按任务书口径：只有 ``owner`` 档
    会产生档位外的人；``private`` / ``all`` 档下候选都算档位内。"""
    if mode == "owner":
        return person_id not in peers
    return False


def _option_items(
    settings: settings_mod.Settings,
    candidates: list[str],
    contacts: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """候选 → 带标注的下拉项：``{id, name, label, is_owner, out_of_scope}``。

    ``id`` 恒为 person_id 本身（前端按 id 去重，改了会炸）；``owner`` 档把主人
    置顶（稳定排序，其余保持原序）；三档候选**数量相同**，只有标注不同。
    """
    mode = _scope_mode(settings)
    peers = {str(peer) for peer in settings.love_peers}
    items = [
        {
            "id": key,
            "name": raw_name(settings, key, contacts),
            "label": display_name(settings, key, contacts),
            "is_owner": key in peers,
            "out_of_scope": _out_of_scope(mode, key, peers),
        }
        for key in candidates
    ]
    if mode == "owner":
        items.sort(key=lambda item: 0 if item["is_owner"] else 1)
    return items


def who_options(*, settings: settings_mod.Settings, proactive_store: Any) -> list[dict[str, Any]]:
    """顶栏"对谁"下拉的候选（裁定 §1.3#3）。

    候选 = ``proactive.contacts`` ∪ ``_conf_schema.love_peers``（**兜底保证至少一项**）。

    这是 ``/status`` 用的**轻量初始候选**——它不收 ``notebook_store``，好让
    ``status_payload`` 的签名保持最小；小本本里的候选由 ``notebook_payload``
    带出的 ``who_options``（``who_options_full``）补齐，前端合并两者。

    第 8 步：每项 ``{id, name, label, is_owner, out_of_scope}``——``label`` 是
    "名字(数字)" 显示串，``is_owner`` / ``out_of_scope`` 跟着 ``scope.mode`` 标注
    （只标注不删候选，三档数量相同）。
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
    return _option_items(settings, candidates, contacts)


def who_options_full(
    *,
    settings: settings_mod.Settings,
    notebook_store: Any = None,
    proactive_store: Any = None,
) -> list[dict[str, Any]]:
    """完整候选集（小本本的键也要算进来）。前端顶栏用这个。

    标注规则同 ``who_options``（第 8 步）：``label`` = "名字(数字)"，owner 档主人
    置顶、档位外的人只标注不删。没给 ``proactive_store`` 时拿不到联系人昵称，
    ``name`` 回落 ``name_preference``，``label`` 同样成立。
    """
    candidates: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)

    contacts: Mapping[str, Any] = {}
    for person_id in settings.love_peers:
        add(person_id)
    if proactive_store is not None:
        try:
            contacts = proactive_store.contacts()
        except Exception:  # noqa: BLE001 - 坏表不能带累面板
            contacts = {}
        for person_id in contacts:
            add(person_id)
    if notebook_store is not None:
        doc = notebook_store.read()
        for person_id in (doc.get("facts") or {}):
            add(person_id)
        for promise in list(doc.get("promises") or []):
            if isinstance(promise, dict):
                add(promise.get("about"))
    return _option_items(settings, candidates, contacts)


# ---- 日记（只读）--------------------------------------------------------------


def _panel_edit_window(settings: settings_mod.Settings | None) -> dict[str, Any]:
    """面板的改删窗口（天）。``within_days <= 0`` 表示**不限**。

    唯一读 ``diary.panel_edit_unlimited`` 这个开关的地方；判据（多旧算"太旧"）
    仍然是 ``fmt.within_window``，这里只把"几天"翻译成数字。

    **拿不到 settings 就按不能改**（``can_edit=False``）——宁可不给按钮，也不能让前端
    在不知道窗口的情况下画出一个按下去必失败的按钮。
    """
    limits = dict(getattr(settings, "diary", {}) or {})
    unlimited = bool(limits.get("panel_edit_unlimited"))
    try:
        within = max(int(limits.get("edit_within_days") or 0), 0)
    except (TypeError, ValueError):
        within = 0
    return {
        "can_edit": settings is not None,
        "unlimited": unlimited,
        "within_days": 0 if unlimited else within,
        "switch": "diary.panel_edit_unlimited",
    }


def diary_list_payload(*, diary_store: Any, now: datetime | None = None) -> dict[str, Any]:
    """两本日记的概览。``love_collapsed`` 恒 ``true``（决策 #18：恋爱日记默认收起）。

    每本另带一份**每日清单** ``days``（第 6.1 步）：前端日历照着画，不用自己再
    解析正文。``first_date`` 一律取**截断后** ``days[0]`` 的日期——截断时单独
    重算"最早一天"就会和 ``days`` 对不上，前端会跳进空月份。
    """
    books: list[dict[str, Any]] = []
    for book in fmt.BOOKS:
        path = diary_store.path(book)
        entries = fmt.parse_entries(diary_store.read(book), book)
        days, truncated = _calendar_days(entries)
        books.append(
            {
                "book": book,
                "display": BOOK_DISPLAY.get(book, book),
                "is_love": book == fmt.LOVE,
                "entries": len(entries),
                "chars": sum(len(entry.text) for entry in entries),
                "updated_at": _mtime_iso(path),
                "latest_date": entries[-1].date if entries else "",
                "days": days,
                "first_date": days[0]["date"] if days else "",
                "days_truncated": truncated,
            }
        )
    return {"ok": True, "books": books, "love_collapsed": True}


def _calendar_days(entries: list[Any]) -> tuple[list[dict[str, Any]], bool]:
    """条目 → ``{date, count, chars}`` 每日一项，按 ``date`` 升序。

    同一天多条聚合成一项（``count`` / ``chars`` 是那天合计）；超过
    ``MAX_CALENDAR_DAYS`` 天只保留**最近的**一段，``days_truncated`` 置真——
    "更早的不在日历里"由前端据此提示。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for entry in entries:
        date = str(getattr(entry, "date", "") or "")
        if not date:
            continue
        bucket = buckets.get(date)
        if bucket is None:
            buckets[date] = {"date": date, "count": 1, "chars": len(entry.text)}
        else:
            bucket["count"] += 1
            bucket["chars"] += len(entry.text)
    days = [buckets[key] for key in sorted(buckets)]
    truncated = len(days) > MAX_CALENDAR_DAYS
    if truncated:
        days = days[-MAX_CALENDAR_DAYS:]
    return days, truncated


def diary_content_payload(
    *,
    diary_store: Any,
    book: str = fmt.NORMAL,
    date: str = "",
    tail: int = 0,
    now: datetime | None = None,
    settings: settings_mod.Settings | None = None,
) -> dict[str, Any]:
    """读一段日记正文。**只读，不重写正文**。

    给 ``date`` 就取那天；否则取最近 ``tail`` 条（``tail<=0`` 时取全部）。

    ``text`` 是给老前端与既有测试的**整段视图**，原样保留；``entries``（第 6.1 步）
    是同一批条目的**结构化视图**——顺序与 ``picked`` 完全一致、条数等于 ``count``，
    ``mood`` / ``who`` 原样透传（昵称替换是前端拿 ``who_options`` 干的活）。
    非空 ``picked`` 时恒有 ``"\\n\\n".join(e["text"]) == text``（测试钉住）。

    第 15 步**只增不改**每个条目加两个键：``seg_id``（改 / 删时指名哪一段，见
    ``format.seg_id``）与 ``editable``（这一段现在能不能改，窗口 + 豁免开关的结论）。
    顶层另加 ``edit_window``，让前端能把"为什么这段不能改"说清楚，也能在设置里
    找到那个开关——**不给它猜第二套规则**。
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
    window = _panel_edit_window(settings)
    moment = now or (datetime.now(settings.zone()) if settings is not None else None)
    zone = settings.zone() if settings is not None else None
    return {
        "ok": True,
        "book": target,
        "date": str(date or ""),
        "count": len(picked),
        "total": total,
        "text": "\n\n".join(entry.text for entry in picked),
        "entries": [
            {
                "date": entry.date,
                "time": entry.time,
                "mood": entry.mood,
                "who": entry.who,
                "chars": len(entry.text),
                "text": entry.text,
                "seg_id": fmt.seg_id(
                    fmt.Segment(
                        head_line=fmt.make_head_line(
                            entry.date, entry.time, entry.mood, entry.who
                        ),
                        head_idx=-1,
                        end_idx=-1,
                        date=entry.date,
                        time=entry.time,
                        mood=entry.mood,
                        who=entry.who,
                        text=entry.text,
                    )
                ),
                "editable": bool(
                    window["can_edit"]
                    and moment is not None
                    and zone is not None
                    and _entry_editable(entry, moment, window["within_days"], zone)
                ),
            }
            for entry in picked
        ],
        "edit_window": window,
    }


def _entry_editable(entry: fmt.Entry, now: datetime, within_days: int, tz: Any) -> bool:
    """这一段现在能不能改。``within_days<=0`` = 不限**多久以前**，但未来段照旧不可改。

    判据与 ``DiaryStore.apply_by_id`` 里那句一字不差（``already_happened`` 是共用
    底线，``within_window`` 是限定期），免得前端把按钮画成能按、后端却说"太旧"。
    """
    seg = fmt.segment_from_record(
        {
            "date": entry.date,
            "time": entry.time,
            "mood": entry.mood,
            "who": entry.who,
            "text": entry.text,
        }
    )
    if not fmt.already_happened(seg, now, tz):
        return False
    return within_days <= 0 or fmt.within_window(seg, now, within_days, tz)


async def rewrite_diary_entry(
    *,
    diary_store: Any,
    settings: settings_mod.Settings,
    book: str,
    seg_id: str,
    text: object,
    now: datetime | None = None,
) -> dict[str, Any]:
    """改一段的**正文**（头行不动）。返回 ``{ok: True, ...}`` 或 ``{ok: False, error}``。

    正文先过 ``fmt.normalize_body(..., max_chars)`` —— 与 ``diary_write`` 同一把尺子，
    免得面板能塞进一条 10 万字正文而聊天侧永远写不出来。

    判据、备份与原子写全在 ``DiaryStore.apply_by_id``（每文件一把锁），这里只做
    形状检查与结果翻译；**不抛异常**（任务书 §2.2）。
    """
    target = book if book in fmt.BOOKS else fmt.NORMAL
    if not str(seg_id or "").strip():
        return {"ok": False, "error": "缺少参数 seg_id"}
    limits = dict(getattr(settings, "diary", {}) or {})
    window = _panel_edit_window(settings)
    body = fmt.normalize_body(text, int(limits.get("max_chars") or 1200))
    if not body:
        return {"ok": False, "error": "正文不能为空（想删掉这一段请用删除）"}
    moment = now or datetime.now(settings.zone())
    result = await diary_store.apply_by_id(
        target,
        seg_id=str(seg_id).strip(),
        action="rewrite",
        new_body=body,
        now=moment,
        within_days=int(window["within_days"]),
        tz=settings.zone(),
    )
    if not result.ok or result.seg is None:
        return {"ok": False, "error": str(result.error or "没改成")}
    return {
        "ok": True,
        "action": "rewrite",
        "book": target,
        "seg": {"date": result.seg.date, "time": result.seg.time, "mood": result.seg.mood},
        # 回**新**正文（``result.seg`` 是落盘前那一段的快照，回它等于把旧内容又报一遍）
        "text": body,
        "before": result.before[:300],
        "chars": len(body),
    }


async def delete_diary_entry(
    *,
    diary_store: Any,
    settings: settings_mod.Settings,
    book: str,
    seg_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """删一段（**先进回收站**，可一键还原）。返回 ``{ok, error}``。

    正文落盘前 ``DiaryStore.apply_by_id`` 会把这一段归档成 ``diary-trash/seg-*.json``；
    还原只把它按时间顺序插回去，不动其余内容。
    """
    target = book if book in fmt.BOOKS else fmt.NORMAL
    if not str(seg_id or "").strip():
        return {"ok": False, "error": "缺少参数 seg_id"}
    window = _panel_edit_window(settings)
    moment = now or datetime.now(settings.zone())
    result = await diary_store.apply_by_id(
        target,
        seg_id=str(seg_id).strip(),
        action="delete",
        now=moment,
        within_days=int(window["within_days"]),
        tz=settings.zone(),
    )
    if not result.ok or result.seg is None:
        return {"ok": False, "error": str(result.error or "没删掉")}
    return {
        "ok": True,
        "action": "delete",
        "book": target,
        "seg": {"date": result.seg.date, "time": result.seg.time, "mood": result.seg.mood},
        "text": result.before[:300],
        "trashed": True,
    }


def diary_trash_payload(*, diary_store: Any, settings: Any = None) -> dict[str, Any]:
    """回收站（删掉的单段记录），**倒序**：刚删的在最上面。

    这里只列 ``seg-*.json``。``diary_edit`` 工具留的整本快照（``*.txt``）**不列**——
    整本回写会连带抹掉这之后的写入，按钮上写"还原"就是在骗人。它还在目录里，
    需要的话自己去捞。
    """
    items: list[dict[str, Any]] = []
    for record in diary_store.trash_items():
        book = str(record.get("book") or "")
        text = str(record.get("text") or "")
        items.append(
            {
                "id": str(record.get("id") or ""),
                "book": book,
                "book_display": BOOK_DISPLAY.get(book, book or "未知"),
                "is_love": book == fmt.LOVE,
                "date": str(record.get("date") or ""),
                "time": str(record.get("time") or ""),
                "mood": str(record.get("mood") or ""),
                "who": str(record.get("who") or ""),
                "chars": len(text),
                "preview": " ".join(text.split())[:80],
                "text": text,
                "deleted_at": str(record.get("deleted_at") or ""),
                "seg_id": str(record.get("seg_id") or ""),
            }
        )
    return {
        "ok": True,
        "count": len(items),
        "cap": TRASH_SEGMENT_CAP,
        "items": items,
        "can_edit": settings is not None,
    }


async def restore_diary_entry(
    *,
    diary_store: Any,
    settings: settings_mod.Settings,
    trash_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """从回收站还原一段（**幂等**：已经在本子里就拒绝，绝不插重）。"""
    key = str(trash_id or "").strip()
    if not key:
        return {"ok": False, "error": "缺少参数 id"}
    moment = now or datetime.now(settings.zone())
    # 记录必须**先**读：还原成功后那条 JSON 就被清掉了，事后再读只会拿到空。
    record = diary_store.trash_record(key) or {}
    ok, why = await diary_store.restore_segment(key, now=moment)
    if not ok:
        return {"ok": False, "error": why or "还原失败"}
    return {
        "ok": True,
        "action": "restore",
        "id": key,
        "book": str(record.get("book") or ""),
        "seg": {
            "date": str(record.get("date") or ""),
            "time": str(record.get("time") or ""),
        },
    }



# ---- 小本本 -------------------------------------------------------------------


def notebook_payload(
    *,
    settings: settings_mod.Settings,
    notebook_store: Any,
    who: str = "",
    proactive_store: Any = None,
) -> dict[str, Any]:
    """小本本（读）。``facts`` / ``promises`` **原样透传 store 条目，键名别改**。

    ``who`` 只是过滤器：面板按"人"组织，不按会话（裁定 §1.3#4）。给空就是全量。
    第 8 步：``who_name`` / ``who_options`` 带上联系人昵称（给了 ``proactive_store``
    才拿得到），另加 ``scope_mode``（前端显示"当前启用范围：只主人"）。
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
    contacts: Mapping[str, Any] | None = None
    if proactive_store is not None:
        try:
            contacts = proactive_store.contacts()
        except Exception:  # noqa: BLE001 - 坏表不能带累面板
            contacts = {}
    return {
        "ok": True,
        "who": person,
        "who_name": display_name(settings, person, contacts=contacts),
        "who_options": who_options_full(
            settings=settings, notebook_store=notebook_store, proactive_store=proactive_store
        ),
        "scope_mode": _scope_mode(settings),
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
    "MAX_CALENDAR_DAYS",
    "affinity_payload",
    "complete_note",
    "delete_diary_entry",
    "diary_content_payload",
    "diary_list_payload",
    "diary_trash_payload",
    "display_name",
    "file_report",
    "forget_note",
    "history_payload",
    "notebook_payload",
    "proactive_payload",
    "raw_name",
    "restore_diary_entry",
    "rewrite_diary_entry",
    "status_payload",
    "who_options",
    "who_options_full",
]
