"""触发器（有什么可说）：七个，各自只回答"现在有没有一件值得说的事"。

产出候选（说给谁、**原文片段**、优先级），**无权决定发不发**——闸门和出站都不在
这一层。两条铁律在这里落地：

- **有内容才发**：候选必须带具体素材（约定原文 / 事实原文 / 那天的日记 / 作息词 /
  真实的时间事实），取不到就返回空——防她自己编内容、退化成打卡；
- **群聊克制**：除约定跟进与纪念日外，其余触发器**只在私聊出**（§2 硬约束）。
  纪念日的内容源 ``facts_for`` 本身只在私聊可见，所以实际能进群的只有约定跟进。

所有目标会话都从 ``contacts``（person → 最后互动的会话，钩子里记录）反查 umo；
对目标人取料（``facts_for`` / ``diary.read_for``）时把他的私聊 ``Session`` 递进去，
第 2/3 步的可见性判定原样生效——恋爱日记的内容只会送到它唯一的读者手里。

阈值全部标**待调**（任务书 §5 工期纪律）：时段窗口是模块常量，``quiet_days`` 进配置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import gate
from ..session import Session

Finder = Callable[..., list["Candidate"]]

PRIORITY_ORDER: tuple[str, ...] = (
    "signal",  # 心情暗号（直发固定字符，独立四道闸，见 api.py）
    "promise",  # 约定跟进
    "anniversary",  # 纪念日
    "care",  # 关怀（劝睡/劝饭）
    "quiet",  # 久未联系
    "morning",  # 早安（早晚安触发器 · 早）
    "goodnight",  # 晚安（早晚安触发器 · 晚）
    "diary",  # 日记驱动
)
"""一 tick 的问询顺序（软内容排最后，频率上限先挤掉最像打卡的那类）。
早晚安在任务书里是一类触发器，slot 拆成两个键是为了"早晚各一次"的当天防重。"""

CARE_SLEEP_WINDOW = ((22, 30), (23, 30))
CARE_MEAL_WINDOWS = (((11, 30), (13, 0)), ((17, 30), (19, 0)))
"""劝睡 / 劝饭窗口（待调）：劝睡压着免打扰前沿（22:30-23:30），23:30 后被闸门挡掉
是正确行为；劝饭=饭点 + 这个人今天还没说过话——没有更强的"没吃饭"信号源，先这样。"""

GREETING_MORNING = ((6, 30), (9, 0))
GREETING_NIGHT = ((23, 0), (24, 0))
"""早晚安窗口（待调）：早安从免打扰结束（默认 06:30）起算；晚安到免打扰开始
（默认 23:30）为止，重叠部分交给闸门挡。目标只有 love_peers 的私聊——名单空＝不发。

晚安那格写成 ``(24, 0)`` = **当天结束**（见 ``_hm``）。原先写 ``(23, 59)`` 配右开区间
实际只覆盖到 23:58:59，最后一分钟是死区——默认 15 分钟一 tick 时碰不到，但
``patrol_minutes=1`` 就会每天空转一分钟，属于"常量说的和做的不是一回事"。"""

DIARY_WINDOW = ((7, 0), (10, 0))
"""日记驱动窗口（待调）：早上读昨天的日记，想接着聊。"""

FRAGMENT_MAX = 60
"""片段截断长度：素材是给她看的提示，不是原文转述，够她"接着说"就行。"""

_DATE_RE = re.compile(
    r"(?:(?P<y1>\d{4})[-/.])?(?:(?P<m1>\d{1,2})[月/-](?P<d1>\d{1,2})日?)"
)
"""日期扫描：``M月D日`` / ``M-D`` / ``YYYY-MM-DD``；年可缺（生日是年历日）。"""

_ANNIVERSARY_KEYWORDS = ("生日", "纪念日", "周年", "节日")
"""先过关键词再提日期——"10月12日要交报告"不是纪念日。"""


@dataclass(frozen=True)
class Candidate:
    """一个候选：说给谁（session/umo）、说什么素材（fragment）、属于哪类（slot）。"""

    slot: str
    umo: str
    session: Session
    fragment: str
    instruction: str
    priority: int = field(default=0)
    """PRIORITY_ORDER 里的下标，越小越优先（由工厂函数统一填）。"""

    @property
    def is_private(self) -> bool:
        return self.session.is_private


def _candidate(slot: str, umo: str, session: Session, fragment: str, instruction: str) -> Candidate:
    return Candidate(
        slot=slot,
        umo=umo,
        session=session,
        fragment=fragment[:FRAGMENT_MAX],
        instruction=instruction,
        priority=PRIORITY_ORDER.index(slot),
    )


def _private_contact_sessions(
    doc: dict[str, Any], allowed: set[str] | None = None
) -> list[tuple[str, str, Session]]:
    """私聊 contacts 的 (person_id, umo, Session)，按插入序稳定遍历。

    ``allowed`` 是过了会话闸的 umo 集合（先闸门后内容）；``None`` ＝ 不限制
    （直调触发器做单测时用）。
    """
    contacts = doc.get("contacts")
    contacts = contacts if isinstance(contacts, dict) else {}
    out: list[tuple[str, str, Session]] = []
    for person_id, contact in contacts.items():
        if not isinstance(contact, dict):
            continue
        if str(contact.get("kind") or "") != "private":
            continue
        umo = str(contact.get("umo") or "")
        if not umo:
            continue
        if allowed is not None and umo not in allowed:
            continue
        out.append((str(person_id), umo, Session.from_umo(umo)))
    return out


def _in_window(moment: datetime, window: tuple[tuple[int, int], tuple[int, int]]) -> bool:
    start = datetime.combine(moment.date(), _hm(window[0]), tzinfo=moment.tzinfo)
    end = datetime.combine(moment.date(), _hm(window[1]), tzinfo=moment.tzinfo)
    return start <= moment < end


def _already_sent(doc: dict[str, Any], umo: str, slot: str, *, now: datetime) -> bool:
    """这个会话今天是否已经发过该类目（``slots_today`` 防重）。

    2 小时最小间隔挡不住"窗口比间隔长"的类目（早安窗 2.5h、纪念日全天）——
    一天一条的约束在这里收口。
    """
    return slot in gate.sent_slots(gate.entry_of(doc, umo), now.date().isoformat())


def _hm(pair: tuple[int, int]):
    """``(时, 分)`` → ``time``；``(24, 0)`` 是**当天最后一刻**的哨兵。

    ``_in_window`` 是右开区间（``start <= t < end``），所以"到当天结束"没法写成
    ``time(24, 0)``（time 不接受 24 点）——用它当哨兵，效果是覆盖到 23:59:59.999999。
    """
    from datetime import time

    if pair[0] == 24 and pair[1] == 0:
        return time(23, 59, 59, 999999)
    return time(pair[0], pair[1])


# ---- 触发器（顺序即优先级，api 层按序问） ----------------------------------------


def find_promises(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """约定跟进：到期/过期的未完成约定（第 2 步 ``promises_due`` 现成），问一句进展。

    唯一允许进群的软内容——约定正文只进 payload.note（给她看的），她自己在注入
    可见性规则（群聊只出计数行）的约束下组织措辞。
    """
    contacts = doc.get("contacts")
    contacts = contacts if isinstance(contacts, dict) else {}
    out: list[Candidate] = []
    for item in deps.notebook.promises_due(now=now):
        about = str(item.get("about") or "")
        contact = contacts.get(about)
        if not isinstance(contact, dict):
            continue
        umo = str(contact.get("umo") or "")
        if not umo:
            continue
        if allowed is not None and umo not in allowed:
            continue
        if _already_sent(doc, umo, "promise", now=now):
            continue
        due = str(item.get("due_at") or "")
        overdue = bool(due and due < now.date().isoformat())
        text = str(item.get("text") or "").strip()
        if not text:
            continue  # 有内容才发：空约定不跟进
        instruction = (
            "这条约定已经过了日子，自然地问问他办得怎么样了"
            if overdue
            else "这条约定就是最近的事，自然地提一句、问问进展，别像催作业"
        )
        out.append(_candidate("promise", umo, Session.from_umo(umo), text, instruction))
        break  # 一 tick 至多问一条约定
    return out


def find_anniversaries(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """纪念日：小本本事实里的生日等（关键词 + 日期扫描，命中今天才触发）。"""
    out: list[Candidate] = []
    for _pid, umo, session in _private_contact_sessions(doc, allowed):
        if _already_sent(doc, umo, "anniversary", now=now):
            continue
        for fact in deps.notebook.facts_for(session):
            text = str(fact.get("text") or "").strip()
            if not text or not any(k in text for k in _ANNIVERSARY_KEYWORDS):
                continue
            if not _date_hits_today(text, now):
                continue
            out.append(
                _candidate(
                    "anniversary",
                    umo,
                    session,
                    text,
                    "今天对TA是个特别的日子（看素材），自然地提一句",
                )
            )
            break  # 每人最多一条
        if out:
            break  # 一 tick 至多一条
    return out


def find_care(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """关怀（劝睡/劝饭）：窗口 + 这个人的互动状态 + 小本本里认识他（有事实才劝）。"""
    affinity = deps.affinity
    out: list[Candidate] = []
    sleep = _in_window(now, CARE_SLEEP_WINDOW)
    meal = any(_in_window(now, window) for window in CARE_MEAL_WINDOWS)
    if not sleep and not meal:
        return out
    for pid, umo, session in _private_contact_sessions(doc, allowed):
        if _already_sent(doc, umo, "care", now=now):
            continue
        facts = deps.notebook.facts_for(session)
        if not facts:
            continue  # 不认识的人不劝（防打卡的第一道闸）
        last_seen = affinity.last_seen(pid, now=now)
        if sleep:
            talked_today = last_seen is not None and last_seen.date() == now.date()
            if not talked_today:
                continue  # 劝睡：今天说过话才劝（知道他现在还醒着）
            fragment = str(facts[0].get("text") or "").strip()
            out.append(
                _candidate(
                    "care", umo, session, fragment, "到点了，自然地劝TA早点休息（结合素材）"
                )
            )
        else:
            if last_seen is not None and last_seen.date() == now.date():
                continue  # 劝饭：今天还没说过话才劝
            fragment = str(facts[0].get("text") or "").strip()
            out.append(
                _candidate("care", umo, session, fragment, "饭点了，顺口问一句TA吃没吃饭")
            )
        break  # 一 tick 至多劝一个人
    return out


def find_quiet(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """久未联系：熟悉度榜上认识的人（泛泛及以上）N 天没来，接上最后一次的话题。"""
    cfg = dict(deps.settings.proactive)
    quiet_days = int(cfg.get("quiet_days") or 3)
    out: list[Candidate] = []
    for pid, umo, session in _private_contact_sessions(doc, allowed):
        if _already_sent(doc, umo, "quiet", now=now):
            continue
        if deps.affinity.band(pid, now=now) == "陌生":
            continue  # 只问候认识的人
        last_seen = deps.affinity.last_seen(pid, now=now)
        if last_seen is None:
            continue
        days = (now.date() - last_seen.date()).days
        if days < quiet_days:
            continue
        # 素材优先用最后互动那天的日记（能接着说"那天你跟我说…"），没有就用时间事实
        fragment = ""
        result = deps.diary.read_for(session, date=last_seen.date().isoformat(), now=now)
        if result.get("ok"):
            text = str(result.get("text") or "").strip()
            if text and not text.startswith("（"):
                fragment = text
        if not fragment:
            fragment = f"你们已经 {days} 天没聊过天了"
        out.append(
            _candidate(
                "quiet",
                umo,
                session,
                fragment,
                "好久没联系了，自然地打个招呼；有日记就接着那天的话聊，别提熟悉度数值",
            )
        )
        break  # 一 tick 至多问候一个人
    return out


def find_greetings(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """早晚安：作息时段 + 只发给 love_peers 的私聊（名单空＝不发，天然克制）。

    早安 / 晚安各是一个 slot（``morning`` / ``goodnight``），当天各只发一次。
    """
    peers = tuple(deps.settings.love_peers or ())
    if not peers:
        return []
    contacts = doc.get("contacts")
    contacts = contacts if isinstance(contacts, dict) else {}
    slot = ""
    if _in_window(now, GREETING_MORNING):
        slot = "morning"
    elif _in_window(now, GREETING_NIGHT):
        slot = "goodnight"
    else:
        return []
    snapshot = deps.state.snapshot(now=now)
    rhythm_word = str((snapshot.get("rhythm") or {}).get("word") or "")
    if not rhythm_word:
        return []  # 作息关了就没有"现在该干嘛"的素材
    out: list[Candidate] = []
    for pid in peers:
        contact = contacts.get(pid)
        if not isinstance(contact, dict) or str(contact.get("kind") or "") != "private":
            continue
        umo = str(contact.get("umo") or "")
        if not umo:
            continue
        if allowed is not None and umo not in allowed:
            continue
        if _already_sent(doc, umo, slot, now=now):
            continue
        instruction = (
            "新的一天开始了，跟TA说声早安"
            if slot == "morning"
            else "到点了，自然地道个晚安，劝TA早点休息"
        )
        out.append(_candidate(slot, umo, Session.from_umo(umo), rhythm_word, instruction))
        break  # 一 tick 至多问候一个人
    return out


def find_diary_hooks(deps: Any, doc: dict[str, Any], *, now: datetime, allowed: set[str] | None = None) -> list[Candidate]:
    """日记驱动：早上翻昨天自己写的日记，想接着聊两句。"""
    if not _in_window(now, DIARY_WINDOW):
        return []
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    out: list[Candidate] = []
    for _pid, umo, session in _private_contact_sessions(doc, allowed):
        if _already_sent(doc, umo, "diary", now=now):
            continue
        result = deps.diary.read_for(session, date=yesterday, now=now)
        if not result.get("ok"):
            continue
        text = str(result.get("text") or "").strip()
        if not text or text.startswith("（"):
            continue  # （这天没写）之类，没有内容
        out.append(
            _candidate(
                "diary", umo, session, text, "你昨天写了日记，接着里面的话自然地聊两句"
            )
        )
        break  # 一 tick 至多一条
    return out


FINDERS: tuple[Finder, ...] = (
    find_promises,
    find_anniversaries,
    find_care,
    find_quiet,
    find_greetings,
    find_diary_hooks,
)
"""常规触发器按优先级排好的问询顺序（暗号不在里面——它走独立四道闸）。"""


def _date_hits_today(text: str, now: datetime) -> bool:
    """文本里的日期是否命中今天（只比月日：生日是年历日；缺省年也命中）。"""
    today = (now.month, now.day)
    for match in _DATE_RE.finditer(text):
        month = match.group("m1")
        day = match.group("d1")
        if month is None or day is None:
            continue
        try:
            if (int(month), int(day)) == today:
                return True
        except ValueError:
            continue
    return False
