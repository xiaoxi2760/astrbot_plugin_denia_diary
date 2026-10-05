"""日记语义入口（第 1 步）：``read_for`` / ``write_for`` / ``edit_for`` / ``prompt_line`` / ``event_hint``。

**读取入口唯一化**：``enabled`` 判定与恋爱日记的可见性都写在这里，适配器不许绕过它直接读文件。
（原包 P1-a 的教训：判定函数写好了却没接线，等于没有。）

适配器只做三件事：把事件翻译成 ``Session``、把这里的返回值套进工具结果、把
``payload.note`` / ``system_prompt`` 拼好。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import storage
from ..session import Session
from . import format as fmt
from .store import DiaryStore

DISABLED = "日记功能已关闭"
ALL_TEXT_LIMIT = 20000
_TAIL_MAX = 200
_SEARCH_MAX = 50
_SCOPE_NOTE_SCOPE = ("group", "private")
_NUDGE_KEEP = 60
_NUDGE_CAP = 200


@dataclass
class Diary:
    """日记门面。配置、布局、文件层都由调用方注入（内核不认平台，也不认全局单例）。"""

    settings: object
    layout: storage.Layout
    store: DiaryStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)

    # ---- 内部：配置、时区、书 -------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self.settings.subsystem("diary"))

    def _tz(self):
        return self.settings.zone()

    def _limits(self) -> dict:
        return dict(self.settings.diary)

    def love_peer(self, session: Session) -> str:
        """这个会话是不是"和某个最亲密的人"的私聊 —— 是就返回他的 id。

        判据用 ``person_id()`` 而不是 ``peer_id()``：主动轮/定时唤醒的事件没有
        sender_id，但私聊的 session_id 就是对端 id；不看这一层兜底的话，
        定时写的那条日记会掉进普通日记那本（真机 2026-10-05 10:00 段）。
        """
        if not session.is_private:
            return ""
        peer = session.person_id()
        return peer if peer and peer in self.settings.love_peers else ""

    def book_for(self, session: Session) -> str:
        """这条日记写哪一本。"""
        return fmt.LOVE if self.love_peer(session) else fmt.NORMAL

    # ---- 内部：读 ------------------------------------------------------------

    def entries(self) -> list[fmt.Entry]:
        out: list[fmt.Entry] = []
        for book in fmt.BOOKS:
            out.extend(fmt.parse_entries(self.store.read(book), book))
        return sorted(out, key=lambda e: f"{e.date} {e.time}")

    def visible(self, entries: list[fmt.Entry], session: Session) -> list[fmt.Entry]:
        """恋爱日记**只对最亲密那个人的私聊可见**；配了名单就一律对其他人/群聊隐藏。

        名单为空时不拦（否则装完没人读得到这本，等于白写）。
        """
        if self.love_peer(session):
            return list(entries)
        if self.settings.love_peers:
            return [e for e in entries if e.book != fmt.LOVE]
        return list(entries)

    # ---- 读：唯一入口 ---------------------------------------------------------

    def read_for(
        self,
        session: Session,
        *,
        date: str = "",
        q: str = "",
        tail: int | None = None,
        all_: bool = False,
        scope: str = "",
        book: str = "",
        mark_love: bool = False,
        now: datetime | None = None,
    ) -> dict:
        """读取入口。四种模式：``date`` / ``q`` / ``all_`` / 默认最近 N 条。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}

        raw = self.entries()
        scoped = self.visible(raw, session)
        book_filtered = _filter_book(scoped, book)

        if date:
            if not _is_day(date):
                return {"ok": False, "error": "date 格式应为 YYYY-MM-DD"}
            day_all = [e for e in book_filtered if e.date == date]
            shown = _filter_scope(day_all, scope)
            text = fmt.render(shown, mark_love=mark_love) if shown else "（这天没写）"
            return {
                "ok": True,
                "date": date,
                "count": len(shown),
                "total": len(book_filtered),
                "text": text + _scope_note(scope, len(day_all), len(shown)),
            }

        if q:
            needle = str(q).strip().lower()
            hits = [
                e
                for e in book_filtered
                if needle in f"{e.date}{e.mood}{e.who}{e.text}".lower()
            ]
            shown = _filter_scope(hits, scope)
            limit = _clamp(tail if tail is not None else 10, 1, _SEARCH_MAX)
            text = (
                fmt.render(shown[:limit], mark_love=mark_love)
                if shown
                else f"（本子里没找到「{q}」）"
            )
            return {
                "ok": True,
                "q": q,
                "count": len(shown[:limit]),
                "total": len(book_filtered),
                "matched": len(hits),
                "text": text + _scope_note(scope, len(hits), len(shown)),
            }

        if all_:
            shown = _filter_scope(book_filtered, scope)
            text = fmt.render(shown, mark_love=mark_love)
            return {
                "ok": True,
                "count": len(shown),
                "total": len(book_filtered),
                "text": (text[:ALL_TEXT_LIMIT] if text else "（这个筛选下没有内容）")
                + _scope_note(scope, len(book_filtered), len(shown)),
            }

        count = _clamp(tail if tail is not None else 20, 1, _TAIL_MAX)
        recent = book_filtered[-count:]
        shown = _filter_scope(recent, scope)
        text = fmt.render(shown, mark_love=mark_love)
        return {
            "ok": True,
            "count": len(shown),
            "total": len(book_filtered),
            "text": (text if shown else "（这个筛选下没有内容）")
            + _scope_note(scope, len(recent), len(shown)),
        }

    # ---- 目录 ----------------------------------------------------------------

    def list_days(
        self, session: Session, *, limit: int = 7, now: datetime | None = None
    ) -> dict:
        """目录：最近几天、各几段、每段开头一句（不吐正文，省 token）。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        entries = self.visible(self.entries(), session)
        by_day: dict[str, list[fmt.Entry]] = {}
        for entry in entries:
            by_day.setdefault(entry.date, []).append(entry)
        days = sorted(by_day.items(), key=lambda item: item[0], reverse=True)[
            : _clamp(limit, 1, 60)
        ]
        return {
            "ok": True,
            "count": len(days),
            "total": len(entries),
            "days": [
                {
                    "date": date,
                    "entries": len(items),
                    "chars": sum(len(item.text) for item in items),
                    "first": " ".join(items[0].text.split())[:40],
                }
                for date, items in days
            ],
        }

    # ---- 写 ------------------------------------------------------------------

    def write_for(
        self,
        session: Session,
        *,
        text: object,
        mood: object = "",
        date: str = "",
        now: datetime | None = None,
    ) -> dict:
        """写一条。返回段数/字数等回执信息。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        limits = self._limits()
        body = fmt.normalize_body(text, int(limits["max_chars"]))
        if not body:
            return {"ok": False, "error": "日记正文不能为空"}
        day = date if _is_day(date) else fmt.day_of(moment)
        clock = fmt.clock_of(moment)
        tag = session.label(dict(self.settings.name_preference))
        book = self.book_for(session)
        return {
            "ok": True,
            "date": day,
            "time": clock,
            "book": book,
            "chars": len(body),
            "tag": tag,
            "_head": (day, clock, mood, tag, body, book),
        }

    async def write_async(self, session: Session, **kwargs) -> dict:
        """``write_for`` 的落盘版（写文件是异步的，见 ``store.append``）。"""
        result = self.write_for(session, **kwargs)
        if not result.get("ok"):
            return result
        day, clock, mood, tag, body, book = result.pop("_head")
        await self.store.append(book, date=day, time=clock, mood=mood, tag=tag, body=body)
        today = [e for e in self.entries() if e.date == day]
        result["entries_today"] = len(today)
        result["total"] = len(self.entries())
        return result

    # ---- 改 / 删 -------------------------------------------------------------

    async def edit_for(
        self,
        session: Session,
        *,
        action: str = "list",
        book: str = "",
        match: str = "",
        text: object = "",
        now: datetime | None = None,
    ) -> dict:
        """``list`` / ``rewrite`` / ``delete``。逐本试，返回最终动的是哪一本。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        within = int(self._limits()["edit_within_days"])
        want = str(book or "").strip().lower()
        candidates = [b for b in fmt.BOOKS if not want or b == want]
        if not candidates:
            return {"ok": False, "error": "book 只能是 normal 或 love"}

        if action == "list":
            segments: list[dict] = []
            for candidate in candidates:
                for seg in fmt.split_segments(self.store.read(candidate)):
                    segments.append(
                        {
                            "book": candidate,
                            "date": seg.date,
                            "time": seg.time,
                            "mood": seg.mood,
                            "who": seg.who,
                            "text": seg.text[:120],
                            "editable": fmt.within_window(
                                seg, moment, within, self._tz()
                            ),
                        }
                    )
            segments.sort(key=lambda s: f"{s['date']} {s['time']}", reverse=True)
            return {
                "ok": True,
                "action": "list",
                "book": want or "both",
                "within_days": within,
                "count": len(segments),
                "segments": segments,
            }

        if action not in ("rewrite", "delete"):
            return {"ok": False, "error": "action 只能是 list / rewrite / delete"}

        informative = ""
        last_error = ""
        for candidate in candidates:
            result = await self.store.apply(
                candidate,
                action=action,
                match=match,
                new_body=str(text or ""),
                now=moment,
                within_days=within,
                tz=self._tz(),
            )
            if result.ok and result.seg is not None:
                which = "恋爱日记" if candidate == fmt.LOVE else "日记"
                verb = "删掉了" if action == "delete" else "改好了"
                return {
                    "ok": True,
                    "action": action,
                    "book": candidate,
                    "seg": {
                        "date": result.seg.date,
                        "time": result.seg.time,
                        "mood": result.seg.mood,
                    },
                    "before": result.before[:300],
                    "text": f"{verb} {result.seg.stamp} 那段（在**{which}**里｜原文已备份）。",
                }
            last_error = result.error or last_error
            # "太旧"比后一本的"没找到"更有信息量（原包实测踩过）
            if "太旧" in (result.error or "") and not informative:
                informative = result.error

        where = "两本日记里" if not want else f"「{'恋爱日记' if want == fmt.LOVE else '日记'}」里"
        if informative:
            return {"ok": False, "error": informative}
        return {
            "ok": False,
            "error": f"{last_error or '没改成'}（{where}都没找到这一段）",
            "hint": "把那段开头几个字放进 match；想看两本各有什么就先 action=list。",
        }

    # ---- 提示 ----------------------------------------------------------------

    def prompt_line(self, session: Session, *, now: datetime | None = None) -> str:
        """每轮唤醒里那一小段：今天写了没 / 上一条是什么 / 到点了才催一句。"""
        if not self._enabled():
            return ""
        moment = now or datetime.now(self._tz())
        limits = self._limits()
        today = fmt.day_of(moment)
        mine = [e for e in self.entries() if e.date == today]
        earlier = [e for e in self.entries() if e.date != today]
        previous = earlier[-1] if earlier else None

        line = f"【日记】{today}：{'今天已写 ' + str(len(mine)) + ' 段' if mine else '今天还没写'}"
        if previous is not None:
            first = " ".join(previous.text.split())[:40]
            line += f"；上一条是 {previous.date}「{first}…」"
        elif mine:
            line += "；这是本子的开头"
        else:
            line += "；本子上还什么都没有"

        hour = moment.hour
        soft = int(limits["soft_nudge_after_hour"])
        night = int(limits["nudge_after_hour"])
        if mine:
            tail = f"\n（今天写了 {len(mine)} 段；想再补两句也随时 diary_write，想翻随时 diary_read。）"
        elif hour >= night:
            tail = (
                f"\n（已经 {hour} 点了、今天还空着 —— 想写两句就写，用 diary_write；"
                "不用长、不用汇报、没人会检查。）"
            )
        elif hour >= soft:
            tail = "\n（今天还空着 —— 有想记的就随手写一两句（diary_write），没有就算了，不用硬凑。）"
        else:
            tail = "\n（你的本子是一本纯文本：想写随时 diary_write，想翻随时 diary_read；没人检查，也没格式要求。）"
        return line + tail + "\n\n"

    def event_hint(
        self,
        session: Session,
        *,
        strong_text: str = "",
        exchange_who: str = "",
        now: datetime | None = None,
    ) -> str:
        """事件驱动的提醒：**只有这一轮真发生了值得记的事**才补一句，且带冷却。"""
        if not self._enabled():
            return ""
        moment = now or datetime.now(self._tz())
        if [e for e in self.entries() if e.date == fmt.day_of(moment)]:
            return ""  # 今天已经写过了：不念
        strong = bool(strong_text)
        if not strong and not exchange_who:
            return ""
        limits = self._limits()
        cooldown = (
            int(limits["event_hint_cooldown_min"]) if strong else int(limits["fallback_hint_cooldown_min"])
        )
        state = storage.read_json(self.layout.diary_nudge, default={})
        sessions = dict(state.get("sessions") or {})
        key = session.umo or session.session_id or "unknown"
        last = (sessions.get(key) or {}).get("at")
        if isinstance(last, (int, float)) and moment.timestamp() * 1000 - last < cooldown * 60_000:
            return ""
        sessions[key] = {"at": int(moment.timestamp() * 1000)}
        if len(sessions) > _NUDGE_CAP:
            newest = sorted(
                sessions.items(), key=lambda kv: kv[1].get("at", 0), reverse=True
            )[:_NUDGE_KEEP]
            sessions = dict(newest)
        try:
            storage.atomic_write_json(self.layout.diary_nudge, {"sessions": sessions})
        except OSError:
            pass  # 存不上不影响对话
        if strong:
            return "（刚才这事要是想留着，也能顺手写进日记：diary_write。）\n"
        return (
            f"（刚跟「{exchange_who}」聊完一轮 —— 要是有什么想留着的，"
            "顺手写进今天的日记也行：diary_write；不想写就算了。）\n"
        )


# ---- 筛选小工具 ---------------------------------------------------------------


def _clamp(value: object, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = low
    return min(max(number, low), high)


def _is_day(value: object) -> bool:
    text = str(value or "")
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    return text.replace("-", "").isdigit()


def _filter_book(entries: list[fmt.Entry], book: str) -> list[fmt.Entry]:
    want = str(book or "").strip().lower()
    if not want or want in ("all", "both", "全部"):
        return list(entries)
    if want in (fmt.LOVE, "恋爱", "恋爱日记"):
        return [e for e in entries if e.book == fmt.LOVE]
    if want in (fmt.NORMAL, "normal", "普通", "普通日记"):
        return [e for e in entries if e.book != fmt.LOVE]
    return list(entries)


def _filter_scope(entries: list[fmt.Entry], scope: str) -> list[fmt.Entry]:
    want = str(scope or "").strip().lower()
    if not want or want in ("all", "全部"):
        return list(entries)
    if want in ("group", "群", "群聊"):
        return [e for e in entries if e.scope == "group"]
    if want in ("private", "私", "私聊"):
        return [e for e in entries if e.scope == "private"]
    return list(entries)


def _scope_note(scope: str, before: int, after: int) -> str:
    want = str(scope or "").strip().lower()
    if want in ("", "all", "全部") or want not in _SCOPE_NOTE_SCOPE:
        return ""
    dropped = max(0, before - after)
    return (
        f"\n（筛选：{scope} —— 另有 {dropped} 段无法归类：它们写在加〔…〕标注之前，本子里没记来源）"
        if dropped
        else ""
    )


__all__ = ["ALL_TEXT_LIMIT", "DISABLED", "Diary"]
