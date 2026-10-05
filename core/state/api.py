"""状态门面（第 3 步）：``observe`` / ``mood_line`` / ``rhythm_line`` /
``is_late_night`` / ``snapshot``。

**判定收在入口内部**（方案 §十 规矩 1）：

- ``enabled`` 开关（``subsystems.state``，情绪/作息/熟悉度三合一共用）：关了
  ``observe`` 拒写、注入行一律空；
- **坐标由她自报**（v0.4 复核 #1，§七#9 已关闭）：``valence`` / ``arousal`` 给了
  才动坐标（-2~+2，越界夹取、不报错、不写 0 覆盖），没给只沉淀原词——系统永不做
  「原词 → 坐标」的映射；
- **两层情绪**：当下（快层，自报驱动，6 小时可见期，坐标按 3 小时半衰期衰减回
  中性——"三小时前有点烦"不该一直挂着）；近期基调（慢层，坐标每次观测向目标
  挪一步地沉淀，7 天半衰期）；
- **基调限幅**（v0.4 复核 #2）：单次最多移动一格、单日最多一格——防"一篇很丧的
  日记把基调拽低一整周"；
- **数值永不进注入**（决策 #10）：``mood_line`` / ``rhythm_line`` 只出词，不出数。

作息是"config 时段表 + 当前时间"的纯函数，**不落盘**；``state.json`` 只存情绪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .. import storage
from ..session import Session
from .store import LAYER_BASELINE, LAYER_NOW, StateStore

DISABLED = "状态系统已关闭"

MOOD_TTL = timedelta(hours=6)
"""当下情绪的可见期：自报后 6 小时内进注入，之后当作"过去了"。"""

MOOD_HALF_LIFE = timedelta(hours=3)
"""当下坐标的半衰期：给暗号阈值 / 快照等数值消费方（注入不走坐标）。"""

BASELINE_HALF_LIFE_DAYS = 7.0
"""近期基调的半衰期（天）：与熟悉度同周期。"""

BASELINE_STEP = 1.0
"""基调单次最多移动一格（坐标量程 -2~+2，一格 = 1.0）。"""

BASELINE_DAILY = 1.0
"""基调单日最多移动一格。"""

MOOD_LINE_MAX = 44
"""情绪句的预算（state 档 50 字里留给它的；作息一行约 10 字）。
超出先砍基调、保当下（§2.6#6 的领域规则留在状态系统内部）。"""

_NEUTRAL = 0.5
"""象限词与基调短语的触发阈值：|坐标| 低于它不算数。"""

_QUADRANT: dict[tuple[str, str], str] = {
    ("+", "+"): "挺开心",
    ("+", "-"): "很平静",
    ("-", "+"): "有点烦躁",
    ("-", "-"): "有点低落",
}
"""坐标 → 语气词。方向是「坐标 → 词」，不是被禁止的「原词 → 坐标」映射。"""


@dataclass
class State:
    """状态门面。配置、布局、文件层都由调用方注入（内核不认平台，也不认全局单例）。"""

    settings: object
    layout: storage.Layout
    store: StateStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)

    # ---- 写（她自报） ---------------------------------------------------------

    async def observe(
        self,
        session: Session,
        *,
        valence: float | int | None = None,
        arousal: float | int | None = None,
        word: object = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """她自报一次心情。``word`` 一定存（原词）；坐标**给了才动**，越界夹取。

        情绪历史（``state_history.jsonl``，任务书 §2.5 v0.7）按行追加：**坐标真的
        变了**才写 ``layer=now`` 行、基调真的挪了才写 ``layer=baseline`` 行——
        同值重复自报只刷新 ``state.json`` 的时间戳，历史文件一行不加。
        """
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        stamp = moment.isoformat(timespec="seconds")
        word_text = str(word or "").strip()
        target_v = _clamp_coord(valence)
        target_a = _clamp_coord(arousal)
        if not word_text and target_v is None and target_a is None:
            return {"ok": False, "error": "什么都没收到：给一个心情词，或给 valence/arousal 打分。"}
        history: list[dict[str, Any]] = []  # 闭包内攒行，state.json 落盘后锁外统一追加

        def update(mood: dict[str, Any]) -> dict[str, Any]:
            mood = _normalise_mood(mood, stamp)
            old_v, old_a = mood["valence"], mood["arousal"]
            if word_text:
                mood["word"] = word_text
                mood["word_at"] = stamp
            if target_v is not None or target_a is not None:
                if target_v is not None:
                    mood["valence"] = target_v
                if target_a is not None:
                    mood["arousal"] = target_a
                mood["updated_at"] = stamp
                mood["baseline"], moved_v, moved_a = _nudge_baseline(
                    mood["baseline"], target_v, target_a, moment
                )
                if (target_v is not None and target_v != old_v) or (
                    target_a is not None and target_a != old_a
                ):
                    history.append(
                        _history_line(
                            stamp, LAYER_NOW, mood["valence"], mood["arousal"], mood["word"]
                        )
                    )
                if moved_v or moved_a:
                    baseline = mood["baseline"]
                    history.append(
                        _history_line(
                            stamp,
                            LAYER_BASELINE,
                            baseline["valence"],
                            baseline["arousal"],
                            mood["word"],
                        )
                    )
            return mood

        mood = await self.store.update_mood(update)
        await self.store.append_history(history, now=moment)
        return {"ok": True, "word": str(mood.get("word") or ""), "at": stamp}

    # ---- 注入文案（compose 经 StateLike 调用） ---------------------------------

    def mood_line(self, session: Session, *, now: datetime | None = None) -> str:
        """情绪一句话：近期基调 + 当下，两层合成（"这周她心情不错，不过这会儿有点烦"）。

        不含任何数字；没内容可返回空串。超预算**先砍基调、保当下**。
        """
        if not self._enabled():
            return ""
        moment = now or datetime.now(self._tz())
        mood = self.store.read_mood()

        current = self._current_phrase(mood, moment)
        baseline = self._baseline_phrase(mood, moment)
        if current and baseline:
            line = f"最近她{baseline}，不过这会儿{current}。"
        elif current:
            line = f"这会儿她{current}。"
        elif baseline:
            line = f"最近她{baseline}。"
        else:
            return ""
        if len(line) > MOOD_LINE_MAX and current:
            line = f"这会儿她{current}。"  # 先砍基调、保当下
        return line

    def rhythm_line(self, *, now: datetime | None = None) -> str:
        """作息一行：config 时段表 → 状态词（纯函数，不落盘）。空表返回空串。"""
        if not self._enabled():
            return ""
        moment = now or datetime.now(self._tz())
        word = self._rhythm_word(moment)
        return f"现在这个点，她{word}" if word else ""

    def is_late_night(self, *, now: datetime | None = None) -> bool:
        """是否深夜档（config ``state.late_night``，可跨午夜）。喂关怀触发器与免打扰。"""
        if not self._enabled():
            return False  # 状态系统关了，下游触发器也不该拿到深夜信号
        moment = now or datetime.now(self._tz())
        window = _parse_window(str(dict(self.settings.state).get("late_night") or ""))
        if window is None:
            return False
        start, end = window
        minutes = moment.hour * 60 + moment.minute
        if start == end:
            return False
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end  # 跨午夜

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        """纯数据（坐标已按各自半衰期衰减到当前时刻），供 WebUI 与触发器；不含注入文案。"""
        if not self._enabled():
            return {}
        moment = now or datetime.now(self._tz())
        mood = self.store.read_mood()
        updated = _parse_iso(str(mood.get("updated_at") or ""))
        base = dict(mood.get("baseline") or {})
        base_updated = _parse_iso(str(base.get("updated_at") or ""))
        return {
            "mood": {
                "word": str(mood.get("word") or ""),
                "valence": _decayed_coord(
                    mood.get("valence"), updated, moment, MOOD_HALF_LIFE
                ),
                "arousal": _decayed_coord(
                    mood.get("arousal"), updated, moment, MOOD_HALF_LIFE
                ),
                "updated_at": str(mood.get("updated_at") or ""),
                "baseline": {
                    "valence": _decayed_coord(
                        base.get("valence"), base_updated, moment,
                        timedelta(days=BASELINE_HALF_LIFE_DAYS),
                    ),
                    "arousal": _decayed_coord(
                        base.get("arousal"), base_updated, moment,
                        timedelta(days=BASELINE_HALF_LIFE_DAYS),
                    ),
                    "updated_at": str(base.get("updated_at") or ""),
                },
            },
            "rhythm": {
                "word": self._rhythm_word(moment),
                "late_night": self.is_late_night(now=moment),
            },
        }

    # ---- 内部 ---------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self.settings.subsystem("state"))

    def _tz(self):
        return self.settings.zone()

    def _current_phrase(self, mood: dict[str, Any], moment: datetime) -> str | None:
        """当下那半句：可见期内出原词（没有原词才用象限词），过了可见期就是"过去了"。"""
        word_at = _parse_iso(str(mood.get("word_at") or mood.get("updated_at") or ""))
        if word_at is None or moment - word_at > MOOD_TTL:
            return None
        word = str(mood.get("word") or "").strip()
        if word:
            return word
        return _quadrant_phrase(mood.get("valence"), mood.get("arousal"))

    def _baseline_phrase(self, mood: dict[str, Any], moment: datetime) -> str | None:
        """基调那半句：按 7 天半衰期衰减后的 valence 说话，弱基调不出场。"""
        base = dict(mood.get("baseline") or {})
        updated = _parse_iso(str(base.get("updated_at") or ""))
        value = _decayed_coord(
            base.get("valence"), updated, moment, timedelta(days=BASELINE_HALF_LIFE_DAYS)
        )
        if value is None or abs(value) < _NEUTRAL:
            return None
        return "心情不错" if value > 0 else "心情有点低沉"

    def _rhythm_word(self, moment: datetime) -> str:
        config = dict(self.settings.state)
        table = str(config.get("rhythm_weekend") or "")
        if not table or moment.weekday() < 5:  # 周六日才用周末表，没配就沿用工作日
            table = str(config.get("rhythm") or "")
        return _segment_word(table, moment)


# ---- 情绪坐标小工具 --------------------------------------------------------------


def _normalise_mood(mood: dict[str, Any], stamp: str) -> dict[str, Any]:
    """补齐骨架键（§2.5：键名冻结、可加不能改）——外部读到的一定是完整形状。

    只 ``setdefault`` 不覆盖：老文件 / WebUI 写进来的值原样保留。
    """
    mood = dict(mood)
    mood.setdefault("word", "")
    mood.setdefault("valence", 0)
    mood.setdefault("arousal", 0)
    mood.setdefault("updated_at", stamp)
    baseline = dict(mood.get("baseline") or {})
    baseline.setdefault("valence", 0)
    baseline.setdefault("arousal", 0)
    baseline.setdefault("updated_at", stamp)
    mood["baseline"] = baseline
    return mood


def _clamp_coord(value: object) -> int | None:
    """-2~+2 的整数打分；没给（``None``）原样返回，越界夹取、非法当没给。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return max(-2, min(2, number))


def _quadrant_phrase(valence: object, arousal: object) -> str | None:
    if valence is None or arousal is None:
        return None
    try:
        v = float(valence)
        a = float(arousal)
    except (TypeError, ValueError):
        return None
    if abs(v) < _NEUTRAL and abs(a) < _NEUTRAL:
        return None
    if abs(v) < _NEUTRAL:
        return "有点烦躁" if a > 0 else "有点低落"  # 一轴中性时按另一轴说
    if abs(a) < _NEUTRAL:
        return "挺开心" if v > 0 else "有点低落"
    return _QUADRANT[("+" if v > 0 else "-", "+" if a > 0 else "-")]


def _decayed_coord(
    value: object, updated_at: datetime | None, moment: datetime, half_life: timedelta
) -> float | None:
    """把一个坐标按半衰期衰减到当前时刻（只算不写）。没存过就是 ``None``。"""
    if value is None or updated_at is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    seconds = (moment - updated_at).total_seconds()
    if seconds <= 0:
        return number
    factor = 0.5 ** (seconds / max(half_life.total_seconds(), 1.0))
    return round(number * factor, 3)


def _nudge_baseline(
    baseline: dict[str, Any],
    target_v: int | None,
    target_a: int | None,
    moment: datetime,
) -> tuple[dict[str, Any], float, float]:
    """基调向新观测挪一步：先按半衰期折算存量，再限幅（单次 ≤1 格、当日 ≤1 格）。

    返回 ``(基调, valence 移动量, arousal 移动量)``——**必须用移动量**判断基调动没动：
    存量折算（衰减）会改变数值，比对前后值会把纯衰减误判成移动。
    """
    base = dict(baseline)
    today = moment.date().isoformat()
    if base.get("day") != today:
        base["day"] = today
        base["moved_valence"] = 0.0
        base["moved_arousal"] = 0.0
    updated = _parse_iso(str(base.get("updated_at") or ""))
    half_life = timedelta(days=BASELINE_HALF_LIFE_DAYS)
    moved = {"valence": 0.0, "arousal": 0.0}
    for axis, target, moved_key in (
        ("valence", target_v, "moved_valence"),
        ("arousal", target_a, "moved_arousal"),
    ):
        # 两轴的存量都先折算到当前时刻：updated_at 马上要刷新，
        # 不折算的轴会把"上次更新以来的衰减"永久丢掉
        current = _decayed_coord(base.get(axis), updated, moment, half_life) or 0.0
        base[axis] = round(current, 3)
        if target is None:
            continue
        moved_today = float(base.get(moved_key) or 0.0)
        room = max(0.0, BASELINE_DAILY - moved_today)
        move = max(-BASELINE_STEP, min(BASELINE_STEP, float(target) - current))
        move = max(-room, min(room, move))
        base[axis] = round(current + move, 3)
        base[moved_key] = round(moved_today + abs(move), 3)
        moved[axis] = abs(move)
    base["updated_at"] = moment.isoformat(timespec="seconds")
    return base, moved["valence"], moved["arousal"]


def _history_line(
    ts: str, layer: str, valence: Any, arousal: Any, word: str
) -> dict[str, Any]:
    """一行情绪历史（§2.5 v0.7：键名冻结，``json.dumps(..., ensure_ascii=False)`` 落盘）。"""
    return {"ts": ts, "layer": layer, "valence": valence, "arousal": arousal, "word": word}


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


# ---- 作息表小工具 ----------------------------------------------------------------


def _segment_word(table: str, moment: datetime) -> str:
    """时段表 → 当前状态词。到点的最近一段生效；凌晨归入最后一段（跨天收尾）。"""
    segments: list[tuple[int, str]] = []
    for raw_line in str(table or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("｜", "|").split("|", 1)
        if len(parts) != 2:
            continue
        start = _parse_clock(parts[0].strip())
        word = parts[1].strip()
        if start is not None and word:
            segments.append((start, word))
    if not segments:
        return ""
    segments.sort(key=lambda item: item[0])
    minutes = moment.hour * 60 + moment.minute
    active = segments[-1]
    for start, word in segments:
        if start <= minutes:
            active = (start, word)
        else:
            break
    return active[1]


def _parse_clock(value: str) -> int | None:
    parts = value.split(":")
    if len(parts) != 2 or not all(part.isdigit() and len(part) == 2 for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _parse_window(value: str) -> tuple[int, int] | None:
    """``HH:MM-HH:MM`` → 起止分钟。解析不了返回 ``None``（is_late_night 恒 False）。"""
    parts = str(value or "").strip().split("-")
    if len(parts) != 2:
        return None
    start = _parse_clock(parts[0].strip())
    end = _parse_clock(parts[1].strip())
    if start is None or end is None:
        return None
    return start, end


__all__ = ["DISABLED", "State"]
