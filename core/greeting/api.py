"""早晚安记录（用户发起面）：识别 → 记录 → 喂给注入。

**记录归系统、接话归她**（麦麦 fun_pack 的姿势，与"不替她说话"同源）：他说了
早安/晚安，系统只把事实记下来、并把一句话素材并进互动轨迹档——怎么回应、要不要
调侃他起太晚，都是她自己组织语言。**不拦消息、不发模板**。

判定收在入口内部（方案 §十 规矩 1）：

- ``enabled``（``subsystems.greeting``）关了：``observe`` 不记、``line`` 恒空串；
- **整条消息完全匹配**才认（``classify``）：聊天里随口提到"早安"两个字不会命中——
  宁可漏不可误伤，与出站清洗"``(x)``/``:x:`` 一律不动"同一气质；
- **当日首次为准**：同一天同款问候重复说，只记第一条（sleep_tracker 明说的
  "多次早晚安会刷新时间"的坑，在这里收掉）；冷却内（默认 30 分钟）的重复连事件
  都不算，避免她把同一句问候回应两遍；
- **凌晨归属**：0-4 点的晚安在统计上归前一天（``NIGHT_DAY_BOUNDARY``）——
  "连续 N 天道晚安"数的是"睡前的道别"，不是日历日的把戏；
- **睡眠时长宁缺毋滥**：最近晚安 → 这次早安的间隔落在 1~14 小时才说
  （``SLEEP_MIN_HOURS`` / ``SLEEP_MAX_HOURS``），夜猫子与回笼觉算不准就**不提**，
  绝不给她说错话的素材——"宁可不见不可错认"的同一口径。

与主动消息的联动（双向防重）不在这里做：``observe`` 只回答"记没记上"，调用方
看到 ``ok`` 后调 ``Proactive.mark_slot_today`` 占掉她当天同款的主动问候——
记录归 greeting、出站归 proactive，各管各的。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any

from .. import storage
from ..session import Session
from .store import KEEP_DAYS, KEEP_EVENTS, GreetingStore

DISABLED = "早晚安记录已关闭"

MORNING_PATTERNS_DEFAULT: tuple[str, ...] = (
    r"早[呀吖啊哈安]?[~～。．.！!？?]*",
    r"早上好[呀吖啊哈~～。．.！!？?]*",
    r"起床[啦了咯~～。．.！!]*",
    r"醒[了啦][~～。．.！!]*",
    r"gm",
)
NIGHT_PATTERNS_DEFAULT: tuple[str, ...] = (
    r"晚安[呀吖啊哈安]?[~～。．.！!？?]*",
    r"安安[~～。．.！!]*",
    r"睡[了啦][~～。．.！!]*",
    r"睡觉[了啦][~～。．.！!]*",
    r"去睡[了啦]?[~～。．.！!]*",
    r"gn",
)
"""默认识别词表（取自 gsuid_core YumekoSleep 的指令词，加语气后缀容忍）。
配置里每条是一个正则，**整条消息**完全匹配才算；``gm`` / ``gn`` 依赖
``PATTERN_FLAGS`` 的忽略大小写。"""

PATTERN_FLAGS = re.IGNORECASE
"""正则编译旗标：只为 ``gm`` / ``gn`` 这类拉丁词；对中文无副作用。"""

DUPLICATE_MINUTES_DEFAULT = 30
"""同款问候的冷却（分钟）：冷却内的重复连事件都不算，防止她把同一句回应两遍。"""

NIGHT_DAY_BOUNDARY = 4
"""凌晨归属线：0-4 点的晚安统计上归前一天——连续天数数的是"睡前的道别"。"""

FRESH_WINDOW = timedelta(seconds=120)
""""刚说"的判定窗：``line`` 只在最近一次问候落在这个窗内时出素材——记录完
紧接着的当轮注入命中它，之后的轮次自然过期，零状态。"""

SLEEP_MIN_HOURS = 1.0
SLEEP_MAX_HOURS = 14.0
"""睡眠时长的可信区间：落在区间内才给她提"睡了约 X 小时"，区间外（夜猫子 /
回笼觉 / 数据断档）就不提——宁可不说，不可说错。"""

LINE_MAX = 60
"""注入句的字数上限（trace 档 60 字配额；超长就地截断）。"""


@lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    out: list[re.Pattern[str]] = []
    for text in patterns:
        try:
            out.append(re.compile(text, PATTERN_FLAGS))
        except re.error:  # noqa: BLE001 - 坏正则在 load_settings 已报过，这里只跳过
            continue
    return tuple(out)


def classify(
    text: str,
    *,
    morning: tuple[re.Pattern[str], ...] = (),
    night: tuple[re.Pattern[str], ...] = (),
) -> str:
    """整条消息是不是一句早安/晚安。返回 ``"morning"`` / ``"night"`` / ``""``。

    ``fullmatch``：只想接住"他把这条消息整句用来道问候"，聊天里提到这两个词
    一律不算——识别面宁可小，不能在正常对话里乱触发。
    """
    body = str(text or "").strip()
    if not body:
        return ""
    for pattern in morning:
        if pattern.fullmatch(body):
            return "morning"
    for pattern in night:
        if pattern.fullmatch(body):
            return "night"
    return ""


def effective_day(moment: datetime, kind: str) -> str:
    """事件归属日（``YYYY-MM-DD``）：凌晨 0-4 点的晚安归前一天。"""
    day = moment.date()
    if kind == "night" and moment.hour < NIGHT_DAY_BOUNDARY:
        day = day - timedelta(days=1)
    return day.isoformat()


def _events_of(people: dict[str, Any], person_id: str) -> list[dict[str, Any]]:
    entry = people.get(person_id)
    if not isinstance(entry, dict):
        return []
    events = entry.get("events")
    if not isinstance(events, list):
        return []
    return [dict(event) for event in events if isinstance(event, dict) and event.get("kind")]


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _trim(events: list[dict[str, Any]], *, moment: datetime) -> list[dict[str, Any]]:
    """写入前的裁剪：只留最近 ``KEEP_DAYS`` 天、至多 ``KEEP_EVENTS`` 条。"""
    cutoff = (moment - timedelta(days=KEEP_DAYS)).date().isoformat()
    kept = [event for event in events if str(event.get("date") or "") >= cutoff]
    return kept[-KEEP_EVENTS:]


def _sleep_hours(events: list[dict[str, Any]], morning_ts: datetime) -> float | None:
    """最近一次晚安 → 这次早安的间隔（小时）；没有晚安或落在可信区间外 → ``None``。"""
    nights = [
        _parse_iso(event.get("ts"))
        for event in events
        if event.get("kind") == "night"
    ]
    nights = [ts for ts in nights if ts is not None and ts < morning_ts]
    if not nights:
        return None
    hours = (morning_ts - max(nights)).total_seconds() / 3600
    if hours < SLEEP_MIN_HOURS or hours > SLEEP_MAX_HOURS:
        return None
    return round(hours * 2) / 2  # 半小时步进：口头说"约 7.5 小时"够用


def _streak_of(events: list[dict[str, Any]], kind: str, moment: datetime) -> int:
    """连续天数（纯函数，吃事件列表）：从 ``moment`` 的归属日往回逐日数。"""
    days = {
        str(event.get("date") or "")
        for event in events
        if event.get("kind") == kind
    }
    cursor = effective_day(moment, kind)
    count = 0
    while cursor in days:
        count += 1
        cursor = (datetime.fromisoformat(cursor).date() - timedelta(days=1)).isoformat()
    return count


def _hm_label(ts: datetime) -> str:
    """时刻的口语写法（``7:50``，不补小时前导零）。"""
    return f"{ts.hour}:{ts.minute:02d}"


@dataclass
class Greeting:
    """早晚安门面。配置、布局、文件层都由调用方注入。"""

    settings: object
    layout: storage.Layout
    store: GreetingStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)

    # ---- 写（钩子每轮先过这里） -------------------------------------------------

    async def observe(
        self, session: Session, *, text: str = "", now: datetime | None = None
    ) -> dict[str, Any]:
        """识别并记录一次问候。记上了回 ``{"ok": True, "kind", "date", "streak",
        "sleep_hours"}``；没记上回 ``{"ok": False, "reason"}``（``not_greeting`` /
        ``cooldown`` / ``duplicate`` / ``disabled`` / ``no_person``）。

        判定与写入在**同一个锁内闭包**里完成（冷却与当日的复核以锁内的最新读数
        为准），调用方拿到 ``ok`` 才去联动主动消息——全程不抛异常，记录失败
        顶多少一条数据，不能影响对话。
        """
        if not self._enabled():
            return {"ok": False, "reason": "disabled", "error": DISABLED}
        try:
            who = session.person_id()
            if not who:
                return {"ok": False, "reason": "no_person"}
            moment = now or datetime.now(self._tz())
            cfg = dict(self.settings.greeting)
            cooldown = timedelta(minutes=_as_minutes(cfg.get("duplicate_minutes")))
            kind = classify(
                str(text or ""),
                morning=self._patterns("morning_patterns"),
                night=self._patterns("night_patterns"),
            )
            if not kind:
                return {"ok": False, "reason": "not_greeting"}
            outcome: dict[str, Any] = {}

            def update(doc: dict[str, Any]) -> dict[str, Any] | None:
                people = dict(doc.get("people") or {})
                events = _events_of(people, who)
                same = [event for event in events if event.get("kind") == kind]
                last = _parse_iso(same[-1].get("ts")) if same else None
                if last is not None and (moment - last) < cooldown:
                    outcome["reason"] = "cooldown"
                    return None
                day = effective_day(moment, kind)
                if any(str(event.get("date") or "") == day for event in same):
                    outcome["reason"] = "duplicate"
                    return None
                events.append(
                    {"date": day, "kind": kind, "ts": moment.isoformat(timespec="seconds")}
                )
                entry = dict(people.get(who) or {})
                entry["events"] = _trim(events, moment=moment)
                people[who] = entry
                doc["people"] = people
                outcome["date"] = day
                return doc

            saved = await self.store.update(update)
            if saved is None:
                return {"ok": False, "reason": str(outcome.get("reason") or "error")}
            streak = _streak_of(_events_of(self.store.people(), who), kind, moment)
            result: dict[str, Any] = {
                "ok": True,
                "kind": kind,
                "date": str(outcome["date"]),
                "streak": streak,
            }
            if kind == "morning":
                result["sleep_hours"] = self._sleep_hours_of(who, moment)
            return result
        except Exception as error:  # noqa: BLE001 - 记录失败不能影响对话
            return {"ok": False, "reason": "error", "error": str(error)}

    # ---- 注入素材（compose 经 GreetingLike 调用） --------------------------------

    def line(self, session: Session, *, now: datetime | None = None) -> str:
        """互动轨迹档的一句话，**两层**（用户 2026-10-11 定稿：记录睡眠时间 + 让她知晓）：

        - **当轮素材**（2 分钟新鲜窗内）：他刚说完问候 → "这个人刚说了早安，
          已经连续 N 天了，昨晚睡了约 X 小时。"——接问候用，这是"新闻"，当轮说一次；
        - **全天知晓**：今天记到过早安 → 全天出**背景行**"（背景：这个人今早
          7:50 道过早安，昨晚睡了约 X 小时；已知的事，别主动复述。）"——
          她整天知道他几点起的、睡了多久，但措辞框架明说别转述（daily_life 的
          反复读做法，用户反馈"一直注入容易复读"后采纳）。晚安不全天驻留：
          它作为素材被"明早的睡眠时长"消费。
        其余轮次空串不占预算。超预算就地截断（LINE_MAX）。
        """
        if not self._enabled():
            return ""
        try:
            who = session.person_id()
            if not who:
                return ""
            moment = now or datetime.now(self._tz())
            events = _events_of(self.store.people(), who)
            if not events:
                return ""
            fresh = self._fresh_line(events, moment)
            if fresh:
                return fresh
            return self._today_line(events, moment)
        except Exception:  # noqa: BLE001 - 素材坏了就不出，不能带累注入
            return ""

    def _fresh_line(self, events: list[dict[str, Any]], moment: datetime) -> str:
        """当轮素材：最近一次问候落在新鲜窗内才出（记录完紧接着的当轮命中）。"""
        latest = max(events, key=lambda event: str(event.get("ts") or ""))
        ts = _parse_iso(latest.get("ts"))
        if ts is None or (moment - ts) > FRESH_WINDOW:
            return ""
        kind = str(latest.get("kind") or "")
        if kind == "morning":
            line = f"这个人刚说了早安，已经连续 {_streak_of(events, kind, ts)} 天了"
            hours = _sleep_hours(events, ts)
            if hours is not None:
                line += f"，昨晚睡了约 {hours:g} 小时"
            line += "。"
        elif kind == "night":
            line = f"这个人刚道了晚安，已经连续 {_streak_of(events, kind, ts)} 天了。"
        else:
            return ""
        return line[:LINE_MAX]

    def _today_line(self, events: list[dict[str, Any]], moment: datetime) -> str:
        """全天知晓：今天有早安事件 → 全天给他一行**背景**（不是新闻）。

        反复读的框架（用户 2026-10-11 反馈"一直注入容易复读"，调研后取
        daily_life 的做法）：明说"这是已知背景、别主动复述"——她聊到相关
        话题时才自然用，而不是每轮把这句话当新闻转述一遍。
        """
        today = moment.date().isoformat()
        morning = next(
            (
                event
                for event in sorted(
                    (e for e in events if e.get("kind") == "morning"),
                    key=lambda event: str(event.get("ts") or ""),
                )
                if str(event.get("date") or "") == today
            ),
            None,
        )
        if morning is None:
            return ""
        ts = _parse_iso(morning.get("ts"))
        if ts is None:
            return ""
        line = f"（背景：这个人今早 {_hm_label(ts)} 道过早安"
        hours = _sleep_hours(events, ts)
        if hours is not None:
            line += f"，昨晚睡了约 {hours:g} 小时"
        line += "；已知的事，别主动复述。）"
        return line[:LINE_MAX]

    # ---- 查询（触发器 / 以后的 WebUI 用） ---------------------------------------

    def streak(self, person_id: str, kind: str, *, now: datetime | None = None) -> int:
        """连续天数：从今天（凌晨归属修正后）往回数，每天都有一天同款问候才算连续。"""
        moment = now or datetime.now(self._tz())
        return _streak_of(_events_of(self.store.people(), person_id), kind, moment)

    def last_event(self, person_id: str, kind: str) -> dict[str, Any] | None:
        """某人最近一次同款问候（没有就是 ``None``）。"""
        events = [event for event in _events_of(self.store.people(), person_id) if event.get("kind") == kind]
        if not events:
            return None
        return max(events, key=lambda event: str(event.get("ts") or ""))

    def report(
        self, person_id: str, *, days: int = 7, now: datetime | None = None
    ) -> dict[str, Any]:
        """某人的作息报告（``sleep_check`` 工具用；WebUI 以后也用它）。

        按**归属日**聚合（每晚安 + 每早安一行），睡眠时长按当次早安现算；
        只回最近 ``days`` 天（归属日降序）；连续天数锚定各自最近一次事件的
        那天——今天还没问候也不清零。
        """
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        if not person_id:
            return {"ok": False, "error": "不知道这是谁。"}
        try:
            moment = now or datetime.now(self._tz())
            events = _events_of(self.store.people(), person_id)
            if not events:
                return {
                    "ok": False,
                    "error": "还没记到 TA 的作息——TA 跟你道过早安/晚安才会有记录。",
                }
            by_day: dict[str, dict[str, Any]] = {}
            for event in events:
                day = str(event.get("date") or "")
                if not day:
                    continue
                slot = by_day.setdefault(day, {})
                ts = _parse_iso(event.get("ts"))
                if event.get("kind") == "morning":
                    slot["morning"] = ts
                elif event.get("kind") == "night":
                    slot["night"] = ts
            rows: list[dict[str, Any]] = []
            for day in sorted(by_day, reverse=True)[: max(int(days), 1)]:
                slot = by_day[day]
                morning_ts = slot.get("morning")
                rows.append(
                    {
                        "date": day,
                        "night": _hm_label(slot["night"]) if slot.get("night") else "",
                        "morning": _hm_label(morning_ts) if morning_ts else "",
                        "sleep_hours": _sleep_hours(events, morning_ts)
                        if morning_ts
                        else None,
                    }
                )
            return {
                "ok": True,
                "days": rows,
                "morning_streak": self._anchored_streak(events, "morning"),
                "night_streak": self._anchored_streak(events, "night"),
                "total": len(events),
            }
        except Exception as error:  # noqa: BLE001 - 报告失败给人话，不抛
            return {"ok": False, "error": str(error)}

    def _anchored_streak(self, events: list[dict[str, Any]], kind: str) -> int:
        """连续天数，锚定该类目最近一次事件的那天（而不是今天——今天没问候不清零）。"""
        same = [event for event in events if event.get("kind") == kind]
        if not same:
            return 0
        latest = max(same, key=lambda event: str(event.get("ts") or ""))
        ts = _parse_iso(latest.get("ts"))
        return _streak_of(events, kind, ts) if ts is not None else 0

    # ---- 内部 -------------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self.settings.subsystem("greeting"))

    def _tz(self):
        return self.settings.zone()

    def _patterns(self, key: str) -> tuple[re.Pattern[str], ...]:
        cfg = dict(self.settings.greeting)
        raw = cfg.get(key)
        if not isinstance(raw, (list, tuple)) or not raw:
            raw = MORNING_PATTERNS_DEFAULT if key == "morning_patterns" else NIGHT_PATTERNS_DEFAULT
        return _compiled(tuple(str(item) for item in raw))

    def _sleep_hours_of(self, person_id: str, morning_ts: datetime) -> float | None:
        return _sleep_hours(_events_of(self.store.people(), person_id), morning_ts)


def _as_minutes(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DUPLICATE_MINUTES_DEFAULT


__all__ = [
    "DISABLED",
    "DUPLICATE_MINUTES_DEFAULT",
    "MORNING_PATTERNS_DEFAULT",
    "NIGHT_PATTERNS_DEFAULT",
    "PATTERN_FLAGS",
    "Greeting",
    "GreetingStore",
    "classify",
    "effective_day",
]
