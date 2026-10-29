"""状态渲染（唯一出口）。

方案 §三 的底座约定：**一个渲染函数 + 两个挂载点**——

- 被动轮：适配器把这里的结果追加到 ``req.system_prompt``（``on_llm_request``）；
- 主动轮：cron 唤醒**不触发** ``on_llm_request``，适配器把**同一份**结果拼进 ``payload.note``。

所有系统想说的话都必须经这里合成，**禁止各系统各挂一个钩子各塞一段**。
本模块只做"渲染"（拼文本 + 按预算裁剪），不做查询——跨系统的只读查询在 ``core/sources.py``（第 2 步建）。

注入预算：总量 ≤ ``PROMPT_BUDGET`` 字（含换行）。先给状态、互动轨迹预留预算，
再按话题选少量小本本条目；各段按 ``PRIORITY`` 拼接，放不下的整段跳过，继续尝试短段。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

PROMPT_BUDGET = 400
"""``system_prompt`` 里允许追加的总字数（方案 §三，v0.5 由 300 扩到 400）。"""

RECALL_BUDGET = 180
"""日记续聊素材的硬预算；**不增加总预算**，从既有 400 里切，并把机械催写排到它后面。"""

_NOTE_PROMISE_BUDGET = 140
_NOTE_FACT_BUDGET = 100
_NOTE_PROMISE_BUDGET_WITH_RECALL = 110
_NOTE_FACT_BUDGET_WITH_RECALL = 70
"""有续聊素材时小本本主动让出 60 字：优先保证"接得上话"，而不是把本子塞满。"""

PRIORITY: dict[str, int] = {
    "promise": 30,  # 有界的相关约定优先展示
    "fact": 20,  # 关于当前人的事实 + 熟悉度档位词
    "recall": 16,  # 日记正文续聊素材（历史，含当天）：高于 trace/state，远高于催写
    "trace": 15,  # 互动轨迹（第 3 步新增，插在 fact 与 state 之间）
    "state": 10,  # 情绪 / 作息
    "reminder": 0,  # 提醒类（可省）
    "diary": 0,  # 今天的段数 / 上一条 / 催写（机械提醒，最低）
}


class DiaryLike(Protocol):
    """``compose`` 用到的日记能力（协议而非 import，保持依赖单向）。

    ``prompt_parts`` / ``recall_line`` 是新增能力：旧实现只有 ``prompt_line`` /
    ``event_hint``，compose 用 ``getattr`` 探测，缺了只是少一段素材，不报错。
    """

    def prompt_line(self, session: object, *, now: object = None) -> str: ...

    def recall_line(
        self,
        session: object,
        *,
        query: str = "",
        now: object = None,
        budget: int = RECALL_BUDGET,
    ) -> str: ...

    def prompt_parts(
        self,
        session: object,
        *,
        query: str = "",
        now: object = None,
        recall_budget: int = RECALL_BUDGET,
    ) -> tuple[str, str]: ...

    def event_hint(
        self,
        session: object,
        *,
        strong_text: str = "",
        exchange_who: str = "",
        now: object = None,
    ) -> str: ...


class NotebookLike(Protocol):
    """``compose`` 用到的小本本能力（协议而非 import，保持依赖单向）。

    可见性判定在 ``core/notebook/api.py`` 内部做完：这里拿到的已经是
    "当前会话该念的内容"（事实群聊为空、约定群聊只剩脱敏计数行）。
    """

    def promise_line(self, session: object, *, now: object = None) -> str: ...

    def fact_line(self, session: object, *, now: object = None) -> str: ...


class StateLike(Protocol):
    """``compose`` 用到的状态能力（协议而非 import，保持依赖单向）。

    情绪句里"先砍基调保当下"的预算规则在 ``State.mood_line`` 内部完成；
    这里拿到的已经是能直接注入的短句（不含数字）。
    """

    def mood_line(self, session: object, *, now: object = None) -> str: ...

    def rhythm_line(self, *, now: object = None) -> str: ...


class AffinityLike(Protocol):
    """``compose`` 用到的熟悉度能力（协议而非 import，保持依赖单向）。

    档位词与"关于当前人的事实"同属 fact 档（§2.6 的表：quota 100 含档位词）；
    互动轨迹单独一档，``trace_line`` 在数据缺失时自己整档降级为空串。
    """

    def line(self, session: object, *, now: object = None) -> str: ...

    def trace_line(self, session: object, *, now: object = None) -> str: ...


class GreetingLike(Protocol):
    """``compose`` 用到的早晚安记录能力（协议而非 import，保持依赖单向）。

    ``line`` 只在"他刚说完问候"的当轮出一句话素材（含连续天数/睡眠时长，
    自截到 60 字内），其余轮次返回空串、不占预算。
    """

    def line(self, session: object, *, now: object = None) -> str: ...


class SocialLike(Protocol):
    """``compose`` 用到的群社交能力（协议而非 import，保持依赖单向）。

    ``trace_line`` 只在**群聊**会话出一段"群里最近在聊什么"（群社交任务）；
    私聊**恒为空串**——群素材绝不进私聊提示词，边界收在实现内部。
    """

    def trace_line(self, session: object, *, now: object = None) -> str: ...


@dataclass(frozen=True)
class Section:
    """一段要注入的话。``priority`` 越大越先保留。"""

    name: str
    text: str
    priority: int = 0


def render(sections: Iterable[Section], *, budget: int = PROMPT_BUDGET) -> str:
    """按优先级拼成一段，超预算从低优先级开始砍。

    装不下的整段跳过，继续尝试后面的短段；不截半句话。
    总预算包含段落间和末尾的换行。
    """
    kept: list[str] = []
    used = 0
    for section in sorted(sections, key=lambda item: -item.priority):
        text = (section.text or "").strip("\n")
        if not text:
            continue
        room = budget - used - 1  # 每个保留段末尾的换行也算预算
        if room <= 0:
            break
        if len(text) > room:
            continue
        kept.append(text)
        used += len(text) + 1
    return "\n".join(kept) + ("\n" if kept else "")


def _fit_lines(text: str, budget: int) -> str:
    """Legacy providers can return arbitrary text; only retain complete lines."""
    lines: list[str] = []
    used = 0
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        extra = len(line) + bool(lines)
        if used + extra <= budget:
            lines.append(line)
            used += extra
    return "\n".join(lines)


def compose_prompt(
    diary: DiaryLike,
    session: object,
    *,
    notebook: NotebookLike | None = None,
    state: StateLike | None = None,
    affinity: AffinityLike | None = None,
    greeting: GreetingLike | None = None,
    life: object | None = None,
    social: SocialLike | None = None,
    now: object = None,
    query_text: str = "",
    strong_text: str = "",
    exchange_who: str = "",
    budget: int = PROMPT_BUDGET,
) -> str:
    """当前全部注入内容：小本本（约定 / 事实）+ 日记素材 + 互动轨迹 + 状态 + 提醒。

    ``query_text`` 是**检索用**的当前消息（小本本选题、日记素材相关性都读它）；
    ``strong_text`` 只表示**这一轮有明确事件**（如主动轮候选的真实素材），会转给
    ``event_hint``。两者分开：普通聊天原文只能当 query，不能因为"非空"就触发表记
    提醒。旧调用方只传 ``strong_text`` 时，它继续兼任检索 query（兼容）。

    短文本沿用原来的展示顺序；长记忆只在注入时选取，完整清单仍由 note_list 提供。
    熟悉度档位词并进 fact 档（§2.6 的表：100 字配额含档位词）；
    state 与 affinity 各自的渲染规则留在 ``core/state/`` 内部。

    日记素材（``recall``）单独成档，优先级 16：高于 trace/state，远高于
    reminder/diary。有素材时小本本主动从 140+100 收缩到 110+70，**总预算仍是
    PROMPT_BUDGET**——这是把预算从机械催写挪给续聊，不是扩大注入。
    """
    query = query_text or strong_text
    sections: list[Section] = []
    life_text = life.context_line(now=now, budget=110) if life is not None else ""
    state_text = life_text
    if state is not None:
        state_text = "\n".join(text for text in (
            life_text or state.rhythm_line(now=now), state.mood_line(session, now=now)
        ) if text)
    trace_parts: list[str] = []
    if affinity is not None:
        trace_parts.append(affinity.trace_line(session, now=now))
    if greeting is not None:
        trace_parts.append(greeting.line(session, now=now))
    if social is not None:
        # 群话题（群社交任务）：只在群聊会话出料（私聊恒空串，见 SocialLike）；
        # 排在 trace 档最后——熟悉度与问候是"眼前这个人"的事，优先级更高，
        # 70 字预算不够时整行丢弃，不截半句。
        trace_parts.append(social.trace_line(session, now=now))
    trace_text = _fit_lines("\n".join(text for text in trace_parts if text), 70)
    band = affinity.line(session, now=now) if affinity is not None else ""

    # 续聊素材先取（需要 query），这样它占的额度能从小本本预算里扣掉；旧实现
    # 没有 prompt_parts / recall_line 时自动降级，行为与之前一致。
    recall_budget = min(RECALL_BUDGET, max(0, budget))
    status_line = ""
    recall_text = ""
    have_status = False
    parts = getattr(diary, "prompt_parts", None)
    if callable(parts):
        status_line, recall_text = parts(
            session, query=query, now=now, recall_budget=recall_budget
        )
        have_status = True
    else:
        recall_fn = getattr(diary, "recall_line", None)
        if callable(recall_fn):
            recall_text = recall_fn(
                session, query=query, now=now, budget=recall_budget
            )
    recall_text = _fit_lines(recall_text, recall_budget)
    if "｜" not in recall_text:  # 只剩标题没有正文＝没素材
        recall_text = ""

    # Give state, continuity and recall material room before sampling notes.
    # 有 recall 时最多会同时保留 promise/fact/recall/trace/state 五段，给换行留 6 字；
    # 没有 recall 时沿用原来的 4 字，保持旧行为逐字节不变。
    overhead = 6 if recall_text else 4
    available = max(
        0, budget - len(state_text) - len(trace_text) - len(recall_text) - overhead
    )
    if recall_text:
        promise_budget = min(_NOTE_PROMISE_BUDGET_WITH_RECALL, available)
        fact_budget = min(
            _NOTE_FACT_BUDGET_WITH_RECALL, max(0, available - promise_budget)
        )
    else:
        promise_budget = min(_NOTE_PROMISE_BUDGET, available)
        fact_budget = min(_NOTE_FACT_BUDGET, max(0, available - promise_budget))
    note_fact_budget = max(0, fact_budget - len(band) - bool(band))
    fact_text = ""
    if notebook is not None:
        select = getattr(notebook, "prompt_lines", None)
        if callable(select):
            promise, fact_text = select(
                session, now=now, query=query,
                promise_budget=promise_budget, fact_budget=note_fact_budget,
            )
        else:
            # Keep old protocol implementations and test doubles compatible.
            promise = notebook.promise_line(session, now=now)
            fact_text = notebook.fact_line(session, now=now)
        promise = _fit_lines(promise, promise_budget)
        fact_text = _fit_lines(fact_text, note_fact_budget)
        if promise:
            sections.append(Section("promise", promise, PRIORITY["promise"]))
    fact_parts = [text for text in (fact_text, band) if text]
    if fact_parts:
        sections.append(Section("fact", "\n".join(fact_parts), PRIORITY["fact"]))
    if recall_text:
        sections.append(Section("recall", recall_text, PRIORITY["recall"]))
    if trace_text:
        sections.append(Section("trace", trace_text, PRIORITY["trace"]))
    if state_text:
        sections.append(Section("state", state_text, PRIORITY["state"]))
    hint = diary.event_hint(
        session, strong_text=strong_text, exchange_who=exchange_who, now=now
    )
    if hint:
        sections.append(Section("reminder", hint, PRIORITY["reminder"]))
    if not have_status:
        status_line = diary.prompt_line(session, now=now)
    if status_line:
        sections.append(Section("diary", status_line, PRIORITY["diary"]))
    return render(sections, budget=budget)


__all__ = [
    "PRIORITY",
    "PROMPT_BUDGET",
    "RECALL_BUDGET",
    "AffinityLike",
    "DiaryLike",
    "GreetingLike",
    "NotebookLike",
    "Section",
    "SocialLike",
    "StateLike",
    "compose_prompt",
    "render",
]
