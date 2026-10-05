"""熟悉度（原「好感度」，v0.4 改名）：漏积分器 + 粗档位 + 互动轨迹。

测的是"互动多不多"，不是"喜欢不喜欢"——"喜欢"归小本本的自然语言（决策 #10）。
注入只有档位词（熟稔/泛泛/陌生），数值只给触发器与 WebUI。

参数定稿（§七#6 的待定项，第 3 步拍板）：

- **λ = 0.5，单位 = 21 天（三周）**：返工轮定稿（原 7 天衰减太快）。判据：**一周不聊
  不该掉档，一个月不见才该明显降**——半衰期 7 天时"每天一轮私聊"的平衡分只有
  ≈10.6，正好贴着熟稔线（10）运行，一天不聊就掉档，这是病根；放到 21 天后平衡分
  ≈30.8，一周不聊掉 20.6%（≈24.5，离熟稔线还有 2 倍多），泛泛稳定值（5~9）一周后
  4.0~7.2 也不掉，30 天不见才掉到 1.9~11.4（泛泛掉档、熟稔贴线）——判据成立。
  选 21 不选 14：两者都过判据，但 21 与情绪基调（7 天半衰期）拉开一个量级——
  "熟"是比"心情"慢得多的量；且给"偶尔忙一周"的人更大缓冲。
- **Δ 两档**：私聊 1.0 / 群里被叫醒并接话 0.6。``mention`` 与 ``group`` 同档——
  权重梯子**没有**"群聊普通发言"这一档（复核 #4）：信号源 ``on_llm_request``
  只在她被叫醒时触发，群聊里没被叫醒就没有信号；``group`` 只是"群聊接话"的
  别名，不是更低的第三档。返工轮复核：**维持**——升档速度由 Δ 决定、降档速度由
  半衰期决定，判据只用后者就能满足，动 Δ 会让两个方向纠缠、归因不清。
- **单日增量封顶 3.0**：防一天刷屏霸榜（她不碰数值，决策 #5）。返工轮复核：**维持**
  ——半衰期变长后刷屏者的爬升更平滑，封顶仍是反刷屏的主闸。
- **档位阈值**：熟稔 ≥ 10 / 泛泛 ≥ 3 / 其余 陌生——按新半衰期，"每周聊一两次"
  稳在泛泛（平衡 ≈3~9），"每天私聊"两周到三周进熟稔（平衡 ≈30.8，两周 ≈24）。
  返工轮复核：**维持**，理由同 Δ。所有值**待调**（§3 工期纪律：雏形先取有理由的默认值）。

衰减**按经过时间折算**（复核 #3）：``score ×= λ ** (Δt / 单位)``，绝不"每次
touch 乘一次 λ"。实现是懒衰减：落盘时把上次落盘以来的衰减折进去（touch），
读的时候把读取时刻的衰减现算出来（score / band / top），不写回——落盘频率
再怎么变，数字都可复现。

互动轨迹（``trace_line``，§2.6#4）：``f(affinity.last_ts, proactive.last_sent_at)``
两个时间戳的比较，零新存储。``proactive.json`` 是第 4 步的写入者：文件不存在 /
字段缺失 / 解析失败 → **整档返回空串**（第 3 步阶段必然如此），不报错、不占预算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from .. import storage
from ..session import Session
from .store import AffinityStore

LAMBDA = 0.5
"""衰减系数（半衰比例）。"""

DECAY_UNIT_DAYS = 21.0
"""衰减单位（天）：λ 的三周半衰期（返工轮定稿，判据见文件头；待调）。"""

DELTA_PRIVATE = 1.0
DELTA_MENTION = 0.6
DELTA_GROUP = DELTA_MENTION
"""事件权重只有两档；``group`` 与 ``mention`` 同为"群里被叫醒并接话"。"""

DAILY_CAP = 3.0
"""每人单日增量上限（防刷屏）。"""

BAND_CLOSE = 10.0
BAND_KNOWN = 3.0
"""档位阈值：≥ 10 熟稔，≥ 3 泛泛，其余陌生。"""

BAND_CLOSE_WORD = "熟稔"
BAND_KNOWN_WORD = "泛泛"
BAND_STRANGER_WORD = "陌生"

PENDING_WINDOW_HOURS = 6.0
"""待回窗口（第 4 步任务书 §2/§3.3）：``last_sent_at`` 之后这么长时间内，互动轨迹
只说"她刚找过你"，不演被冷落；配置 ``proactive.pending_window_hours`` 存在时以配置为准。"""

_KINDS = ("private", "mention", "group")


@dataclass
class Affinity:
    """熟悉度门面。配置、布局、文件层都由调用方注入（内核不认平台）。"""

    settings: object
    layout: storage.Layout
    store: AffinityStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)

    # ---- 写 -----------------------------------------------------------------

    async def touch(
        self, session: Session, *, kind: str = "private", now: datetime | None = None
    ) -> None:
        """记一次互动（``on_llm_request`` 触发即计数，那一刻她真的被叫醒了）。"""
        if not self._enabled():
            return
        who = session.person_id()
        if not who:
            return
        moment = now or datetime.now(self._tz())
        delta = self._delta(kind)
        stamp = moment.isoformat(timespec="seconds")
        today = moment.date().isoformat()

        def update(person: dict[str, Any]) -> dict[str, Any]:
            score = _decay(float(person.get("score") or 0.0), person.get("last_ts"), moment)
            gained = float(person.get("gained_today") or 0.0)
            if person.get("day") != today:
                gained = 0.0
            gain = min(delta, max(0.0, DAILY_CAP - gained))
            gained += gain
            return {
                "score": score + gain,
                "last_ts": stamp,
                "day": today,
                "gained_today": gained,
            }

        await self.store.update_person(who, update)

    # ---- 读（衰减在读取时刻现算，不写回） --------------------------------------

    def score(self, person_id: str, *, now: datetime | None = None) -> float:
        """某人的当前分数（漏积分器：已按经过时间衰减）。没记录过就是 0。"""
        person = self._person(person_id)
        if not person:
            return 0.0
        moment = now or datetime.now(self._tz())
        value = _decay(float(person.get("score") or 0.0), person.get("last_ts"), moment)
        return round(max(0.0, value), 4)

    def band(self, person_id: str, *, now: datetime | None = None) -> str:
        """粗档位词：只可能是 ``熟稔`` / ``泛泛`` / ``陌生`` 三者之一。"""
        value = self.score(person_id, now=now)
        if value >= BAND_CLOSE:
            return BAND_CLOSE_WORD
        if value >= BAND_KNOWN:
            return BAND_KNOWN_WORD
        return BAND_STRANGER_WORD

    def line(self, session: Session, *, now: datetime | None = None) -> str:
        """注入用的档位词一行（只有档位词，不含数值）。"""
        if not self._enabled():
            return ""
        who = session.person_id()
        if not who:
            return ""
        word = self.band(who, now=now)
        if word == BAND_CLOSE_WORD:
            return "（这个人和你已经很熟稔了，说话不用见外。）"
        if word == BAND_KNOWN_WORD:
            return "（这个人和你只是泛泛之交。）"
        return "（这个人在你这里还是个陌生人。）"

    def trace_line(self, session: Session, *, now: datetime | None = None) -> str:
        """互动轨迹：她上次主动找他是何时、对方回了没有 + 距上次互动多久。

        数据源是 ``affinity.last_ts`` 与 ``proactive.json`` 的 ``last_sent_at``
        （按会话键），**两个时间戳缺任何一个都整档降级为空串**。

        待回窗口（第 4 步 §3.3）：她发出后 ``pending_window_hours``（默认 6h）内
        对方还没回时，只说"她刚主动找过你"——她刚发完五分钟，不许演被冷落；
        窗口外才允许"你还没回她"。
        """
        if not self._enabled():
            return ""
        who = session.person_id()
        if not who:
            return ""
        person = self._person(who)
        last_ts = _parse_iso(str(person.get("last_ts") or "")) if person else None
        last_sent = self._last_sent_at(session)
        if last_ts is None or last_sent is None:
            return ""
        moment = now or datetime.now(self._tz())
        answered = last_ts >= last_sent
        in_pending = moment - last_sent < self._pending_window()
        if not answered and in_pending:
            line = (
                f"她{_rel_word(last_sent, moment)}刚主动找过你，"
                f"你们上次聊天是{_rel_word(last_ts, moment)}。"
            )
        else:
            line = (
                f"她{_rel_word(last_sent, moment)}主动找过你，"
                + ("后来你们又聊过。" if answered else "你还没回她。")
                + f"你们上次聊天是{_rel_word(last_ts, moment)}。"
            )
        return line[:60]

    def _pending_window(self) -> timedelta:
        """待回窗口时长：配置优先（``proactive.pending_window_hours``），缺省用常量。"""
        hours = PENDING_WINDOW_HOURS
        cfg = getattr(self.settings, "proactive", None)
        if isinstance(cfg, Mapping):
            try:
                hours = float(cfg.get("pending_window_hours") or PENDING_WINDOW_HOURS)
            except (TypeError, ValueError):
                pass
        return timedelta(hours=max(hours, 0.0))

    def top(self, *, limit: int = 5, now: datetime | None = None) -> list[dict[str, Any]]:
        """互动最多的几个人（分数降序，衰减后 ≤ 0 的不出场）。WebUI 与触发器用。"""
        moment = now or datetime.now(self._tz())
        ranked: list[dict[str, Any]] = []
        for person_id, person in self.store.read_people().items():
            value = _decay(float(person.get("score") or 0.0), person.get("last_ts"), moment)
            if value > 0:
                ranked.append({"id": person_id, "score": round(value, 4)})
        ranked.sort(key=lambda item: (-item["score"], item["id"]))
        limit = max(int(limit), 0)
        return ranked[:limit]

    def last_seen(self, person_id: str, *, now: datetime | None = None) -> datetime | None:
        """某人最后一次互动的时刻（第 4 步触发器用：久未联系 / 关护判定）。没记录过是 None。"""
        person = self._person(person_id)
        if not person:
            return None
        return _parse_iso(str(person.get("last_ts") or ""))

    # ---- 内部 ---------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self.settings.subsystem("state"))

    def _tz(self):
        return self.settings.zone()

    def _person(self, person_id: str) -> dict[str, Any] | None:
        if not person_id:
            return None
        return self.store.read_people().get(person_id) or None

    def _last_sent_at(self, session: Session) -> datetime | None:
        """读 ``proactive.json`` 的 ``last_sent_at``（mtime 缓存；缺失/损坏一律 None）。"""
        doc = storage.read_json(self.layout.proactive, default={})
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        if not isinstance(sessions, dict):
            return None
        entry: Any = None
        for key in (session.umo, session.session_id):
            if key and isinstance(sessions.get(key), dict):
                entry = sessions[key]
                break
        if not isinstance(entry, dict):
            return None
        return _parse_iso(str(entry.get("last_sent_at") or ""))

    @staticmethod
    def _delta(kind: object) -> float:
        name = str(kind or "").strip().lower()
        if name == "private":
            return DELTA_PRIVATE
        if name in ("mention", "group"):
            return DELTA_MENTION
        return DELTA_GROUP  # 未认识的 kind 按"群聊接话"计，不丢信号


# ---- 小工具 --------------------------------------------------------------------


def _decay(score: float, last_ts: object, moment: datetime) -> float:
    """``score ×= λ ** (Δt / 单位)``：按经过时间折算（复核 #3）。"""
    last = _parse_iso(str(last_ts or ""))
    if last is None:
        return score
    elapsed_days = (moment - last).total_seconds() / 86400
    if elapsed_days <= 0:
        return score
    return score * (LAMBDA ** (elapsed_days / DECAY_UNIT_DAYS))


def _parse_iso(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # 没有时区的时间戳没法算 Δt，宁可不算
    return parsed


def _rel_word(moment: datetime, now: datetime) -> str:
    days = (now.date() - moment.date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days == 2:
        return "前天"
    return f"{days}天前"


__all__ = [
    "BAND_CLOSE",
    "BAND_KNOWN",
    "BAND_CLOSE_WORD",
    "BAND_KNOWN_WORD",
    "BAND_STRANGER_WORD",
    "DAILY_CAP",
    "DECAY_UNIT_DAYS",
    "DELTA_GROUP",
    "DELTA_MENTION",
    "DELTA_PRIVATE",
    "LAMBDA",
    "PENDING_WINDOW_HOURS",
    "Affinity",
]
