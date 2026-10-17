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

PRIORITY: dict[str, int] = {
    "promise": 30,  # 有界的相关约定优先展示
    "fact": 20,  # 关于当前人的事实 + 熟悉度档位词
    "trace": 15,  # 互动轨迹（第 3 步新增，插在 fact 与 state 之间）
    "state": 10,  # 情绪 / 作息
    "reminder": 0,  # 提醒类（可省）
    "diary": 0,
}


class DiaryLike(Protocol):
    """``compose`` 用到的日记能力（协议而非 import，保持依赖单向）。"""

    def prompt_line(self, session: object, *, now: object = None) -> str: ...

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
    now: object = None,
    strong_text: str = "",
    exchange_who: str = "",
    budget: int = PROMPT_BUDGET,
) -> str:
    """当前全部注入内容：小本本（约定 / 事实）+ 互动轨迹 + 状态（作息 / 情绪）+ 日记段。

    短文本沿用原来的展示顺序；长记忆只在注入时选取，完整清单仍由 note_list 提供。
    熟悉度档位词并进 fact 档（§2.6 的表：100 字配额含档位词）；
    state 与 affinity 各自的渲染规则留在 ``core/state/`` 内部。

    早晚安素材并进 **trace 档**（它本来就是"连续性"的题中之义）：问候句与
    互动轨迹句拼在同一档，一起吃 trace 的预算与砍序——不新增第六类目，
    PROMPT_BUDGET 与 PRIORITY 一个字节不动。
    """
    sections: list[Section] = []
    state_text = ""
    if state is not None:
        state_text = "\n".join(text for text in (
            state.rhythm_line(now=now), state.mood_line(session, now=now)
        ) if text)
    trace_parts: list[str] = []
    if affinity is not None:
        trace_parts.append(affinity.trace_line(session, now=now))
    if greeting is not None:
        trace_parts.append(greeting.line(session, now=now))
    trace_text = _fit_lines("\n".join(text for text in trace_parts if text), 70)
    band = affinity.line(session, now=now) if affinity is not None else ""
    # Give state and continuity room before sampling notes. Notebook entries
    # consume at most 140 + 100 chars, even when no state provider is installed.
    available = max(0, budget - len(state_text) - len(trace_text) - 4)
    promise_budget = min(140, available)
    fact_budget = min(100, max(0, available - promise_budget))
    note_fact_budget = max(0, fact_budget - len(band) - bool(band))
    fact_text = ""
    if notebook is not None:
        select = getattr(notebook, "prompt_lines", None)
        if callable(select):
            promise, fact_text = select(
                session, now=now, query=strong_text,
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
    if trace_text:
        sections.append(Section("trace", trace_text, PRIORITY["trace"]))
    if state_text:
        sections.append(Section("state", state_text, PRIORITY["state"]))
    hint = diary.event_hint(
        session, strong_text=strong_text, exchange_who=exchange_who, now=now
    )
    if hint:
        sections.append(Section("reminder", hint, PRIORITY["reminder"]))
    line = diary.prompt_line(session, now=now)
    if line:
        sections.append(Section("diary", line, PRIORITY["diary"]))
    return render(sections, budget=budget)


__all__ = [
    "PRIORITY",
    "PROMPT_BUDGET",
    "AffinityLike",
    "DiaryLike",
    "GreetingLike",
    "NotebookLike",
    "Section",
    "StateLike",
    "compose_prompt",
    "render",
]
