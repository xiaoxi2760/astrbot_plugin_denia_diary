"""登记 bot 的短聊闸门：结束条件一处生效，防互相唤醒与无限重放。

**轮数按什么计**（任务书要求明确）：1 轮 = "bot 一条叫她的消息 → 她的一次回话"。
唤醒决策（``action="wake"``）落下的那一刻就计一轮——和主动消息的两段式同一口径：
配额在决策时扣，发送失败不回滚（可读原因进决策记录）。默认 ``max_rounds=3``：
她最多连回 3 条，第 4 条 bot 消息就被"轮数上限"拦下并结束会话。

**会话与冷却**：

- 会话由第一条"登记 bot 叫她"的消息开启，持续到：轮数用完 / bot 超过
  ``session_window_minutes`` 没再说话（下次消息到达时惰性关闭）/ 主人关群；
- 会话结束后进入 ``session_cooldown_minutes`` 冷却，同一个群不立刻开新会话；
- **同消息去重**：指纹（说话人 + 归一化正文）留在 ``recent_hashes``（每群最多
  ``HASH_KEEP`` 条，被真实新消息自然挤出）。被唤醒/被"轮数封口"的消息都会记指纹，
  同一段 bot 消息无论重放多少遍都命中去重，**不能**把停下的会话重新唤起；
  冷却期被拒的消息**不**记指纹——它没得到回话，冷却后同文本重试是合法的。

**判定顺序**（每一步留 trail，面板可解释"为何没回"）：
登记 → 是 bot → 允许互动 → 叫她（``self_names``）→ 免打扰 → 去重 → 会话/轮数。
前四步不过的事件**不落任何状态**（未登记的路人刷屏不会写脏文件）。

只与**已登记身份**互动：未登记、登记为人、没被允许互动的，一律不唤醒。
全部纯函数：状态进、状态出，锁与落盘在 ``Social`` 门面层做。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .store import HASH_KEEP

EXCERPT_MAX = 60
"""bot 消息进 note 的摘要长度：素材是提示不是转述（与 triggers.FRAGMENT_MAX 同量级）。"""

SELF_NAME_SEPARATORS = "、，,;；/|"
"""``self_names`` 认的分隔符（顿号 / 逗号 / 分号 / 斜杠 / 竖线，换行与空白也算）。"""


def parse_self_names(raw: Any) -> tuple[str, ...]:
    """``"小澄、澄澄"`` → ``(小澄, 澄澄)``：去空、去重、保序；空串 = 空（短聊不开）。"""
    text = str(raw or "").replace("\n", " ")
    for sep in SELF_NAME_SEPARATORS + ", \t":
        text = text.replace(sep, ",")
    out: list[str] = []
    for piece in text.split(","):
        name = piece.strip()
        if name and name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class ChatDecision:
    """一次短聊判定的结果。``action="wake"`` 才会唤醒她。"""

    action: str  # "wake" | "ignore"
    reason: str = ""
    round: int = 0
    fragment: str = ""
    bot_id: str = ""
    bot_name: str = ""
    trail: tuple[tuple[str, str], ...] = ()

    @property
    def wake(self) -> bool:
        return self.action == "wake"


def empty_chat_state() -> dict[str, Any]:
    return {"active": None, "cooldown_until": "", "recent_hashes": []}


def evaluate(
    state: Any,
    *,
    bot_id: str,
    bot_name: str,
    text: str,
    participant: dict[str, Any] | None,
    self_names: tuple[str, ...] | list[str],
    max_rounds: int,
    session_window_minutes: int,
    session_cooldown_minutes: int,
    now: datetime,
    late_night: bool = False,
) -> tuple[dict[str, Any], ChatDecision]:
    """判定"这条 bot 消息要不要让她回"→ ``(新状态, 决定)``。

    ``state`` 是该群在 ``chats`` 里的条目（缺就当新群）。返回的新状态**总是**可以
    直接落盘；调用方在 ``action=="wake"`` 时负责唤醒（走既有 ``add_active_job``
    通道，回执沿用 ``confirm_sent``）。
    """
    trail: list[tuple[str, str]] = []
    base = state if isinstance(state, dict) else {}
    current = {
        "active": dict(base["active"]) if isinstance(base.get("active"), dict) else None,
        "cooldown_until": str(base.get("cooldown_until") or ""),
        "recent_hashes": [str(h) for h in (base.get("recent_hashes") or [])][:HASH_KEEP],
    }

    def reject(reason: str) -> tuple[dict[str, Any], ChatDecision]:
        trail.append(("拦截", reason))
        return current, ChatDecision(
            action="ignore", reason=reason, bot_id=bot_id, bot_name=bot_name,
            trail=tuple(trail),
        )

    # 1-3 只与已登记身份互动
    if participant is None:
        trail.append(("登记", "对方没有登记"))
        return reject("TA 还没有登记，不互动")
    if not participant.get("is_bot"):
        trail.append(("登记", "对方登记为人，不是 bot"))
        return reject("只与登记为 bot 的身份短聊")
    if not participant.get("allow_interaction"):
        trail.append(("允许", "主人没有开放与 TA 的互动"))
        return reject("主人没有允许与 TA 互动")
    trail.append(("登记", f"已登记 bot：{bot_name or bot_id}"))

    # 4 她在不在被叫
    names = [str(n) for n in (self_names or ()) if str(n)]
    if not names:
        return reject("没有配置她在群里的称呼（self_names），短聊停用")
    body = " ".join(str(text or "").split())
    hit = next((name for name in names if name and name in body), "")
    if not hit:
        return reject("这条消息没有叫她")
    trail.append(("称呼", f"命中称呼「{hit}」"))

    # 5 免打扰：深夜不短聊（也不落指纹——明早同一条消息还可以正常开场）
    if late_night:
        return reject("免打扰时段，深夜不短聊")
    trail.append(("免打扰", "当前不在深夜时段"))

    # 6 同消息去重：先查指纹；**记录**推迟到真正产生动作的分支（唤醒 / 轮数封口）——
    # 冷却期被拒的消息没有获得回话，冷却后同文本重试是合法的，不该被指纹挡住。
    # 而"轮数封口"与"唤醒"都动了会话状态，这些消息必须记指纹，重放才不能再唤起。
    from . import topics

    digest = topics.message_hash(bot_id, body)
    if digest in current["recent_hashes"]:
        return reject("重复消息（去重），不能借同一段话重新唤起")

    def remember() -> None:
        current["recent_hashes"] = (current["recent_hashes"] + [digest])[-HASH_KEEP:]

    trail.append(("去重", "新消息"))

    # 7 会话与轮数
    rounds_max = max(1, int(max_rounds))
    window_min = max(1, int(session_window_minutes))
    cooldown_min = max(0, int(session_cooldown_minutes))
    active = current["active"]
    if active is not None:
        if str(active.get("bot_id") or "") != str(bot_id):
            return reject("正在和别的 bot 短聊，这条先不理")
        if _minutes_since(active.get("last_message_at"), now) > window_min:
            current["active"] = None
            current["cooldown_until"] = (now + timedelta(minutes=cooldown_min)).isoformat(
                timespec="seconds"
            ) if cooldown_min else ""
            trail.append(("会话", f"超过 {window_min} 分钟没有下文，会话已结束"))
            active = None
        elif int(active.get("rounds") or 0) >= rounds_max:
            remember()
            current["active"] = None
            current["cooldown_until"] = (now + timedelta(minutes=cooldown_min)).isoformat(
                timespec="seconds"
            ) if cooldown_min else ""
            trail.append(("轮数", f"已到 {rounds_max} 轮上限，会话结束并冷却"))
            return current, ChatDecision(
                action="ignore",
                reason=f"短聊已到 {rounds_max} 轮上限，自己停下",
                bot_id=bot_id, bot_name=bot_name, trail=tuple(trail),
            )
    if current["active"] is None:
        until = str(current.get("cooldown_until") or "")
        remaining = _cooldown_remaining(until, now)
        if remaining > 0:
            return reject(f"会话冷却中（约剩 {remaining} 分钟）")
        remember()
        current["active"] = {
            "bot_id": str(bot_id),
            "bot_name": str(bot_name or ""),
            "started_at": now.isoformat(timespec="seconds"),
            "last_message_at": now.isoformat(timespec="seconds"),
            "rounds": 1,
        }
        trail.append(("会话", f"新会话第 1 轮（上限 {rounds_max}）"))
        return current, _wake_decision(body, bot_id, bot_name, round=1, max_rounds=rounds_max, trail=trail)

    remember()
    nxt = int(current["active"].get("rounds") or 0) + 1
    current["active"]["rounds"] = nxt
    current["active"]["last_message_at"] = now.isoformat(timespec="seconds")
    trail.append(("会话", f"第 {nxt} 轮（上限 {rounds_max}）"))
    return current, _wake_decision(body, bot_id, bot_name, round=nxt, max_rounds=rounds_max, trail=trail)


def _wake_decision(
    body: str, bot_id: str, bot_name: str, *, round: int, max_rounds: int,
    trail: list[tuple[str, str]],
) -> ChatDecision:
    return ChatDecision(
        action="wake",
        reason=f"第 {round}/{max_rounds} 轮，回一句就停" if round >= max_rounds else f"第 {round}/{max_rounds} 轮",
        round=int(round),
        fragment=f"{bot_name or bot_id}说：{body[:EXCERPT_MAX]}",
        bot_id=str(bot_id),
        bot_name=str(bot_name or ""),
        trail=tuple(trail),
    )


def _minutes_since(value: Any, now: datetime) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        return 0.0  # 没时区的时间戳没法比，当"刚刚说过"处理（宁可少回不可误开）
    return (now - moment).total_seconds() / 60


def _cooldown_remaining(until: str, now: datetime) -> int:
    text = str(until or "").strip()
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0
    if moment.tzinfo is None:
        return 0
    seconds = (moment - now).total_seconds()
    return max(0, int(-(-seconds // 60)))  # 向上取整到分钟


__all__ = [
    "ChatDecision",
    "EXCERPT_MAX",
    "SELF_NAME_SEPARATORS",
    "empty_chat_state",
    "evaluate",
    "parse_self_names",
]
