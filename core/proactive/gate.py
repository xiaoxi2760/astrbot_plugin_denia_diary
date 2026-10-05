"""闸门（该不该说）：三条硬约束 + 各级冷却，一处生效，**不关心内容**。

执行顺序是先闸门后内容（复核 #2）：闸门先过（今日配额 / 免打扰 / 冷却）→
按优先级依次问触发器"你有内容吗" → 取第一个有的发。巡检 15 分钟一天 96 次，
先问内容意味着每次都要翻日记原文、算到期约定——顺序不能反。

免打扰优先于一切常规内容（含劝睡：劝睡窗与深夜档重叠的部分本来就被闸门挡掉，
这是**正确行为**——23:30 之后连劝睡都不该发）。

心情暗号走**独立的四道闸**（决策 #16：破免打扰，但四道闸保证稀有性）：

1. 只发 ``love_peers`` 名单内的**私聊**（双方约定的信号，不是打扰）；
2. 当下 valence 落到最低档——判**衰减后**的坐标（``state.snapshot()``），语义是
   "她**现在**还在难受"；阈值 -0.7 的理由：自报 -2 后约 4.5 小时内、自报 -1 后
   约 1.5 小时内判中（半衰期 3h），窗口盖住"这一阵都撑不住"而不是"刚自报完那一秒"；
   从没给过坐标的期间不触发（宁缺毋滥）；
3. 独立冷却（默认 3 天）；
4. 每天最多一次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SIGNAL_VALENCE = -0.7
"""暗号的"最低档"阈值（判衰减后当下 valence，待调）：理由见模块 docstring。"""


def entry_of(doc: dict[str, Any], umo: str) -> dict[str, Any]:
    """某个会话的状态条目（没有就是空 dict）。"""
    entry = (doc.get("sessions") or {}).get(umo)
    return entry if isinstance(entry, dict) else {}


def today_count(entry: dict[str, Any], today: str) -> int:
    """今日已扣的配额数（``today_date`` 不是今天就是 0——跨天自动重置，不必改文件）。"""
    if str(entry.get("today_date") or "") != today:
        return 0
    try:
        return max(0, int(entry.get("today_count") or 0))
    except (TypeError, ValueError):
        return 0


def sent_slots(entry: dict[str, Any], today: str) -> set[str]:
    """今天已经用过的 slot 集合（同 slot 当天防重：早晚安/约定跟进一天只发一次）。"""
    slots = entry.get("slots_today")
    if not isinstance(slots, dict):
        return set()
    value = slots.get(today)
    return set(value) if isinstance(value, list) else set()


def gate_check(
    *,
    entry: dict[str, Any],
    kind: str,
    cfg: dict[str, Any],
    late_night: bool,
    now: datetime,
) -> str | None:
    """常规内容的闸门。返回 ``None`` ＝ 放行；字符串 ＝ 拦下原因（进日志）。

    ``kind`` 是目标会话的类型（``private`` / ``group``）；``late_night`` 由调用方
    传 ``state.is_late_night()`` 的结果——闸门不认识 State 门面，只认结论。
    """
    if late_night:
        return "免打扰时段（深夜不主动发）"
    today = now.date().isoformat()
    count = today_count(entry, today)
    key = "daily_limit_group" if kind == "group" else "daily_limit_private"
    limit = int(cfg.get(key) or 0)
    if count >= limit:
        return f"今日配额已满（{count}/{limit}）"
    last_sent = _parse_iso(entry.get("last_sent_at"))
    if last_sent is not None:
        elapsed_min = (now - last_sent).total_seconds() / 60
        interval = int(cfg.get("min_interval_minutes") or 0)
        if elapsed_min < interval:
            return f"距上次主动不足 {interval} 分钟"
    return None


def signal_gate(
    *,
    person_id: str,
    love_peers: tuple[str, ...] | list[str],
    snapshot_valence: float | None,
    entry: dict[str, Any],
    cfg: dict[str, Any],
    now: datetime,
) -> str | None:
    """暗号的独立四道闸。返回 ``None`` ＝ 四道全过；字符串 ＝ 第一道拦下的原因。"""
    if not person_id or person_id not in love_peers:
        return "对方不在最亲密名单"
    if snapshot_valence is None:
        return "她从没自报过情绪坐标（暗号宁缺毋滥）"
    if snapshot_valence > SIGNAL_VALENCE:
        return f"当下 valence（{snapshot_valence}）未到最低档"
    today = now.date().isoformat()
    if str(entry.get("signal_date") or "") == today:
        return "今天已经发过暗号"
    last_signal = _parse_iso(entry.get("signal_last_at"))
    if last_signal is not None:
        cooldown = int(cfg.get("signal_cooldown_days") or 3)
        elapsed_days = (now - last_signal).total_seconds() / 86400
        if elapsed_days < cooldown:
            return f"暗号冷却中（{elapsed_days:.1f}/{cooldown} 天）"
    return None


def _parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # 没时区的时间戳没法算间隔，宁可当没有
    return parsed
