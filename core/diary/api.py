"""日记语义入口（第 1 步）：``read_for`` / ``write_for`` / ``edit_for`` / ``prompt_line`` / ``event_hint``。

**读取入口唯一化**：``enabled`` 判定与恋爱日记的可见性都写在这里，适配器不许绕过它直接读文件。
（原包 P1-a 的教训：判定函数写好了却没接线，等于没有。）

适配器只做三件事：把事件翻译成 ``Session``、把这里的返回值套进工具结果、把
``payload.note`` / ``system_prompt`` 拼好。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import storage
from ..session import Session
from . import format as fmt
from .store import DiaryStore

DISABLED = "日记功能已关闭"
ALL_TEXT_LIMIT = 20000
EARLY_HOURS_END = 6
"""凌晨到几点为止算「新的一天刚开头」——**只决定那句话怎么写，不碰任何日期归属**。

用户 2026-10-06 裁定（D8 走"只改措辞"）:00:00~05:59 之间日历已经翻页，但人的这一天还
没开始（默认作息表第一段就是 `06:00|刚醒`），这时候说"今天还没写"读起来像在怪她一整天
没动笔。所以这一段的措辞换成"新的一天刚开头，还没写"。

⚠️ **没有引入"逻辑日"**：`day_of` / `slots_today` / 配额 / 熟悉度衰减一律照旧按日历日算，
这里一个字节都不影响——改的只是给人看（准确说是给她看）的那句话。
"""
_TAIL_MAX = 200
_SEARCH_MAX = 50
_SCOPE_NOTE_SCOPE = ("group", "private")
_NUDGE_KEEP = 60
_NUDGE_CAP = 200
_WRITE_BRAKE = 3
"""当天写到这么多段，注入提醒就改口「不用再写」——纯提示层的刹车，不拦着写（她
真想写还是写得进）。反馈「写日记频率偏高」（2026-10-11）后的第一道闸；要硬上限
另行拍板再加。"""
RECALL_BUDGET = 180
"""被动轮「续聊素材」硬预算（字，含标题与换行）。

它和 ``compose.PROMPT_BUDGET``（400）不是一回事：这里只约束日记素材这一段，
最终仍由 compose 的全局渲染按总预算裁剪。上限写死在入口里，调用方传再大也不放大。
"""
_RECALL_HEADER = "【日记·历史素材（以前写的，不是现在）】"
_RECALL_TAIL = "（全文可 diary_read）"
_RECALL_ITEMS = 2
"""最多引两段：一段最近、一段相关/次近。再多就会从"回忆线索"变成"翻旧账"。"""
_RECALL_RECENT_DAYS = 7
"""次近条目没有关键词命中时，只在这个天数内补一段；更旧的只认明确词面命中。"""


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
            out.extend(self.store.read_entries(book))
        return sorted(out, key=lambda e: f"{e.date} {e.time}")

    def entries_of(self, day: str, *, person_id: str = "") -> list[fmt.Entry]:
        """某一天的条目。``person_id`` 非空就**只看这个人的**（第 17 步）。

        查不到归属的旧段落（头行没记 id）**一律排除**——它们不归属任何人，硬塞给谁都
        算错认领。``person_id`` 留空＝不过滤（全量），给的是面板那边选中的那个人。
        """
        items = self.entries()
        want = str(person_id or "")
        if want:
            items = [e for e in items if e.person_id == want]
        return [e for e in items if e.date == day]

    def _own_entries(self, entries: list[fmt.Entry], session: Session) -> list[fmt.Entry]:
        """按人筛（第 17 步口径；2026-10-11 用户定稿：**私聊看日记只看自己的**）。

        只留 ``person_id == 眼前这个人`` 的段落；没有归属的旧段落**不硬塞**——宁可
        不见，不可错认。``person_id`` 取不到（异常事件）就什么都看不到，而不是
        看到全部——fail-closed。所有**聊天侧读出口**（read_for / read_day_for /
        list_days / prompt_line / event_hint）都从这一个函数过，谁也不许自己另写
        一套筛选。
        """
        pid = str(session.person_id() or "")
        if not pid:
            return []
        return [e for e in entries if e.person_id == pid]

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

    def read_day_for(self, session: Session, *, date: str, book: str = "") -> dict:
        """结构化读一天（给 /看日记 的图片卡用）：返回条目而不是渲染文本。

        可见性与 ``read_for`` **完全同源**（同一套 ``visible`` / ``_filter_book``）——
        恋爱日记的口子不会因为换了个入口而放宽；调用方拿到的就是"这个会话能看的那部分"。
        """
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        if not _is_day(date):
            return {"ok": False, "error": "date 格式应为 YYYY-MM-DD"}
        # 卡片是"发出去的一张图"：同一本里不同人的条目必须分开（第 17 步）。
        # 私聊里只给"写给眼前这个人"的那些；群聊走不到这里（上层先拦了）。
        shown_all = _filter_book(
            self.visible(self._own_entries(self.entries(), session), session), book
        )
        day = [e for e in shown_all if e.date == date]
        return {
            "ok": True,
            "date": date,
            "count": len(day),
            "total": len(shown_all),
            "entries": [
                {
                    "time": e.time,
                    "mood": e.mood,
                    "who": e.who,
                    "book": e.book,
                    "person_id": e.person_id,
                    # 分段口径与面板一致：按空行切段（段内保留换行）
                    "paragraphs": [p for p in re.split(r"\n\s*\n", e.text) if p.strip()],
                }
                for e in day
            ],
        }

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
        """读取入口。四种模式：``date`` / ``q`` / ``all_`` / 默认最近 N 条。

        **只读得到"写给眼前这个人"的段落**（2026-10-11 定稿）：与 ``read_day_for``
        同一个 ``_own_entries``——``diary_read`` 工具在谁的私聊里被调，翻到的就是
        谁的日记，别人的内容不出现在结果里。
        """
        if not self._enabled():
            return {"ok": False, "error": DISABLED}

        raw = self.visible(self._own_entries(self.entries(), session), session)
        book_filtered = _filter_book(raw, book)

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
        """目录：最近几天、各几段、每段开头一句（不吐正文，省 token）。**按人筛**
        （``_own_entries``）——目录里的"每段开头一句"也是内容，不能跨人漏。
        """
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        entries = self.visible(self._own_entries(self.entries(), session), session)
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
        person_id = str(session.person_id() or "")
        book = self.book_for(session)
        return {
            "ok": True,
            "date": day,
            "time": clock,
            "book": book,
            "chars": len(body),
            "tag": tag,
            "person_id": person_id,
            "_head": (day, clock, mood, tag, body, book, person_id),
        }

    async def write_async(self, session: Session, **kwargs) -> dict:
        """``write_for`` 的落盘版（写文件是异步的，见 ``store.append``）。"""
        result = self.write_for(session, **kwargs)
        if not result.get("ok"):
            return result
        day, clock, mood, tag, body, book, person_id = result.pop("_head")
        await self.store.append(
            book, date=day, time=clock, mood=mood, tag=tag, body=body, person_id=person_id
        )
        # ⚠️ 只解析**一次**（第 17 步）：`self.entries()` 会把两本日记整个读回来重新
        # 解析一遍。原来这里调两次、``prompt_line`` 里也调两次——十年数据下每轮要多付
        # 几十毫秒（见 tools/bench_diary.py）。
        all_entries = self.entries()
        result["entries_today"] = len([e for e in all_entries if e.date == day])
        result["total"] = len(all_entries)
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
                for entry in self.store.read_entries(candidate):
                    seg = fmt.entry_segment(entry)
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
        return self._prompt_line_from(
            self.visible(self._own_entries(self.entries(), session), session),
            session,
            moment,
        )

    def _prompt_line_from(
        self,
        entries: list[fmt.Entry],
        session: Session,
        moment: datetime,
        *,
        include_previous: bool = True,
    ) -> str:
        """``prompt_line`` 的纯渲染部分（entries 已按人筛过，避免重复解析）。"""
        limits = self._limits()
        if not session.is_private:
            # 群聊连"今天写了几段"都不把恋爱日记算进来（内容与计数同一口径）。
            entries = [e for e in entries if e.book != fmt.LOVE]
        today = fmt.day_of(moment)
        mine = [e for e in entries if e.date == today]
        earlier = [e for e in entries if e.date != today]
        # 2026-10-04 起：群聊不再念"上一条"的正文。群里的 sender_id 是发言者，
        # 按人筛会把他的私聊日记翻出来——同一个群 ≠ 该人在场。
        # 有完整 recall 素材时也不重复这 40 字（同一段正文只出现一次）。
        previous = (
            earlier[-1] if earlier and session.is_private and include_previous else None
        )

        # D8（用户裁定"只改措辞"）：凌晨那句别说成"今天还没写"——那读起来像在怪她
        # 一整天没动笔，可这时候她的一天还没开始（作息行还在说"该睡了"）。
        # 只影响措辞，日期归属照旧（见 EARLY_HOURS_END）。
        if mine:
            status = "今天已写 " + str(len(mine)) + " 段"
        elif moment.hour < EARLY_HOURS_END:
            status = "新的一天刚开头，还没写"
        else:
            status = "今天还没写"
        line = f"【日记】{today}：{status}"
        if previous is not None:
            first = " ".join(previous.text.split())[:40]
            line += f"；上一条是 {previous.date}「{first}…」"
        elif not mine and not earlier:
            line += "；本子上还什么都没有"
        # 其余情况状态句已经够用：不拿"本子的开头"之类的话占续聊预算。

        hour = moment.hour
        soft = int(limits["soft_nudge_after_hour"])
        night = int(limits["nudge_after_hour"])
        if mine:
            if len(mine) >= _WRITE_BRAKE:
                tail = f"\n（今天已经写了 {len(mine)} 段，够了——有话直接回她，不用再写。）"
            else:
                tail = f"\n（今天写了 {len(mine)} 段；没新事就不用再写，先照常回话。）"
        elif hour >= night:
            tail = (
                f"\n（已经 {hour} 点了、今天还空着 —— 想写两句就写，用 diary_write；"
                "不用长、不用汇报、没人会检查，一段就够。）"
            )
        elif hour >= soft:
            tail = "\n（今天还空着 —— 有想记的就随手写一两句（diary_write），没有就算了，不用硬凑。）"
        else:
            tail = (
                "\n（你的本子想写随时 diary_write，想翻随时 diary_read；没人检查，"
                "也没格式要求。一天一两段就很好，鸡毛蒜皮不用记。）"
            )
        return line + tail + "\n\n"

    def recall_line(
        self,
        session: Session,
        *,
        query: str = "",
        now: datetime | None = None,
        budget: int = RECALL_BUDGET,
    ) -> str:
        """从**当前私聊归属人自己的日记**里取一小段续聊素材（含当天）。

        与 ``prompt_line`` 的分工：那句是"今天写没写"的状态/提醒，这段是"上次聊到
        哪了"的历史正文。两者分开注入，compose 才能让素材优先于机械催写。

        边界（都在本入口内判定，调用方绕不过）：

        - 只给**私聊**；群聊一律空（同一个群 ≠ 该人在场）；
        - 只给 ``_own_entries`` 筛过的段落，再走 ``visible``（恋爱日记权限同源）；
        - 只**逐字引用**日记正文（压成一行、超长加省略号），不总结、不补写；
        - 标题明说"以前写的，不是现在"，防止把旧状态当成当下；
        - 返回长度 ≤ ``budget``，且 **≤ RECALL_BUDGET**（调用方传大也不放大）。
        """
        if not self._enabled() or not session.is_private:
            return ""
        moment = now or datetime.now(self._tz())
        return self._recall_line_from(
            self.visible(self._own_entries(self.entries(), session), session),
            session,
            moment,
            query=query,
            budget=budget,
        )

    def prompt_parts(
        self,
        session: Session,
        *,
        query: str = "",
        now: datetime | None = None,
        recall_budget: int = RECALL_BUDGET,
    ) -> tuple[str, str]:
        """一次解析同时给出 ``prompt_line`` 与 ``recall_line``（compose 的合并入口）。

        两段都要看同一批条目；分开调会把两本日记各解析两遍。旧调用方仍可单独调
        ``prompt_line`` / ``recall_line``，这里只是给 compose 省一次 IO。
        """
        if not self._enabled():
            return "", ""
        moment = now or datetime.now(self._tz())
        entries = self.visible(self._own_entries(self.entries(), session), session)
        recall = self._recall_line_from(
            entries, session, moment, query=query, budget=recall_budget
        )
        return (
            self._prompt_line_from(
                entries, session, moment, include_previous=not recall
            ),
            recall,
        )

    def _recall_line_from(
        self,
        entries: list[fmt.Entry],
        session: Session,
        moment: datetime,
        *,
        query: str = "",
        budget: int = RECALL_BUDGET,
    ) -> str:
        """``recall_line`` 的纯渲染部分；entries 已按人筛过。"""
        if not session.is_private:
            return ""
        try:
            limit = min(max(int(budget), 0), RECALL_BUDGET)
        except (TypeError, ValueError):
            limit = RECALL_BUDGET
        if limit <= len(_RECALL_HEADER) + 8:
            return ""
        rows = [e for e in entries if (e.text or "").strip()]
        if not rows:
            return ""
        rows.sort(key=lambda e: e.stamp)
        latest = rows[-1]
        chosen = [latest]

        # 次近/相关的一段：先看词面命中（最长公共片段 ≥2 字），没有命中就只在
        # 最近 7 天里补一段。这样"昨晚面试→今天问还记得吗"能同时拿到两段，
        # 又不会为了凑数把几个月前无关的旧段落翻出来。
        candidates: list[tuple[int, float, fmt.Entry]] = []
        for entry in rows:
            if entry is latest:
                continue
            score = _recall_score(query, entry.text)
            age = _recall_age_days(entry, moment, self._tz())
            candidates.append((score, age, entry))
        candidates.sort(key=lambda item: (item[0], -item[1], item[2].stamp), reverse=True)
        for score, age, entry in candidates:
            if score >= 2 or age <= _RECALL_RECENT_DAYS:
                chosen.append(entry)
                break
        chosen = chosen[:_RECALL_ITEMS]
        chosen.sort(key=lambda e: e.stamp)
        prefix = {id(entry): f"{entry.date} {entry.time}｜" for entry in chosen}
        header_cost = len(_RECALL_HEADER) + 1
        prefix_cost = sum(len(prefix[id(entry)]) + 1 for entry in chosen)
        body_total = limit - header_cost - prefix_cost
        # 极端小预算先保最新一条；不要为了凑两段把正文挤成 0 字。
        while len(chosen) > 1 and body_total < 12:
            chosen = chosen[-1:]
            prefix = {id(entry): f"{entry.date} {entry.time}｜" for entry in chosen}
            prefix_cost = sum(len(prefix[id(entry)]) + 1 for entry in chosen)
            body_total = limit - header_cost - prefix_cost
        if body_total <= 0:
            return ""

        # 先给最新一条分额度，再给较早/相关的一条：长旧日记不能把当天结果挤掉。
        # 每条先拿均分；短的那条用不完的额度自然留给另一条。
        ordered = sorted(chosen, key=lambda e: e.stamp, reverse=True)
        bodies: dict[int, str] = {}
        remaining = body_total
        for index, entry in enumerate(ordered):
            left = len(ordered) - index
            share = remaining if left == 1 else max(8, remaining // left)
            body = _recall_clip(entry.text, min(share, remaining), query)
            if body:
                bodies[id(entry)] = body
                remaining -= len(body)

        lines = [_RECALL_HEADER]
        for entry in chosen:
            body = bodies.get(id(entry))
            if not body:
                continue
            lines.append(prefix[id(entry)] + body)
        if len(lines) == 1:
            return ""  # 只有标题没有正文＝没素材
        if len("\n".join(lines)) + 1 + len(_RECALL_TAIL) <= limit:
            lines.append(_RECALL_TAIL)
        return "\n".join(lines)

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
        if [
            e
            for e in self._own_entries(self.entries(), session)
            if e.date == fmt.day_of(moment)
        ]:
            return ""  # 这个人今天已经写过了：不念
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
            return (
                "（刚才这事要是想留着，也能顺手写进日记：diary_write。"
                "不用每件事都记，更别让它打断回话。）\n"
            )
        return (
            f"（刚跟「{exchange_who}」聊完一轮 —— 要是有什么想留着的，"
            "顺手写进今天的日记也行：diary_write；不想写就算了，也别为这个打断回话。）\n"
        )


# ---- 筛选小工具 ---------------------------------------------------------------


def _clamp(value: object, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = low
    return min(max(number, low), high)


def _recall_match_span(query: object, text: object) -> tuple[int, int] | None:
    """query 与正文的最长公共片段在 text 里的 ``(start, end)``；不足 2 字返回 None。"""
    q = "".join(str(query or "").split()).lower()[:60]
    raw = str(text or "")
    # 匹配时忽略空白，但窗口要切原文。保留坐标映射，避免英文空格、段落换行
    # 把命中位置向前挪；lower() 展开的字符也映回原字符。
    chars, offsets = [], []
    for index, char in enumerate(raw):
        if not char.isspace():
            lowered = char.lower()
            chars.extend(lowered)
            offsets.extend([index] * len(lowered))
    t = "".join(chars)
    if len(q) < 2 or not t:
        return None
    best = (0, 0)
    for start in range(len(q)):
        end = start + 2
        while end <= len(q) and q[start:end] in t:
            if end - start > best[1] - best[0]:
                at = t.find(q[start:end])
                if at >= 0:
                    best = (at, at + end - start)
            end += 1
    return (offsets[best[0]], offsets[best[1] - 1] + 1) if best[1] > best[0] else None


def _recall_score(query: object, text: object) -> int:
    """query 与日记正文的最长公共片段长度（只用于挑素材，不做语义推断）。

    用最长公共片段而不是关键词表：至少 2 字连续命中才算相关，单字噪声不参与；
    真正的"差不多/还记得吗"这类无词面线索，则由"最近 7 天"兜底。
    """
    q = "".join(str(query or "").split()).lower()[:60]
    t = "".join(str(text or "").split()).lower()
    if len(q) < 2 or not t:
        return 0
    # 便宜的前置过滤：连一个 2 字片段都没有，就不做后面的公共子串计算。
    if not any(q[i : i + 2] in t for i in range(len(q) - 1)):
        return 0
    span = _recall_match_span(q, t)
    return 0 if span is None else span[1] - span[0]


def _recall_age_days(entry: fmt.Entry, moment: datetime, tz) -> float:
    """条目距现在多少天；时间戳坏了就当"很旧"（只有词面命中才可能被选中）。"""
    when = fmt.parse_stamp(entry.date, entry.time, tz)
    if when is None:
        return float("inf")
    return max(0.0, (moment - when).total_seconds() / 86400)


def _recall_clip(text: object, limit: int, query: object = "") -> str:
    """压成一行并截到 limit 字（含省略号）；有词面命中时优先展示命中附近。

    长日记的结尾可能才是当前话题（"……小猫团子下周一要去复查"）。只取开头会把
    真正相关的原文切掉，所以这里以最长公共片段为中心开窗；窗口两侧确有截断时
    才加省略号，保证返回长度 ≤ limit。
    """
    flat = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(flat) <= limit:
        return flat
    span = _recall_match_span(query, flat)
    if span is None:
        return flat[: limit - 1] + "…"
    start, end = span
    if end - start >= limit:
        return flat[start : start + limit]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    room = limit - len(prefix) - len(suffix)
    if room < end - start:
        prefix = suffix = ""
        room = limit
    left = min(start, max(0, (room - (end - start)) // 2))
    right = min(len(flat) - end, room - (end - start) - left)
    left = min(start, room - (end - start) - right)
    window = flat[start - left : end + right]
    return prefix + window + suffix


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


__all__ = ["ALL_TEXT_LIMIT", "DISABLED", "RECALL_BUDGET", "Diary"]
