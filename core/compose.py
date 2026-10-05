"""状态渲染（唯一出口）。

方案 §三 的底座约定：**一个渲染函数 + 两个挂载点**——

- 被动轮：适配器把这里的结果追加到 ``req.system_prompt``（``on_llm_request``）；
- 主动轮：cron 唤醒**不触发** ``on_llm_request``，适配器把**同一份**结果拼进 ``payload.note``。

所有系统想说的话都必须经这里合成，**禁止各系统各挂一个钩子各塞一段**。
本模块只做"渲染"（拼文本 + 按预算裁剪），不做查询——跨系统的只读查询在 ``core/sources.py``（第 2 步建）。

注入预算：总量 ≤ ``PROMPT_BUDGET`` 字，按优先级从高到低保留，超限从低优先级开始砍
（第 3 步起总量 400、五个类目，砍序见 ``PRIORITY``；"先砍基调保当下"这类类目内部的
领域规则留在各自的系统里，这里只做整档的拼接与兜底）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

PROMPT_BUDGET = 400
"""``system_prompt`` 里允许追加的总字数（方案 §三，v0.5 由 300 扩到 400）。"""

PRIORITY: dict[str, int] = {
    "promise": 30,  # 相关约定（不可省）
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


@dataclass(frozen=True)
class Section:
    """一段要注入的话。``priority`` 越大越先保留。"""

    name: str
    text: str
    priority: int = 0


def render(sections: Iterable[Section], *, budget: int = PROMPT_BUDGET) -> str:
    """按优先级拼成一段，超预算从低优先级开始砍。

    - 已经保留了内容、又装不下下一段 → 直接停（不把段落切一半）；
    - 第一段就超过预算 → 截断到预算（总比什么都没有强）；
    - 空文本的段直接跳过。
    """
    kept: list[str] = []
    used = 0
    for section in sorted(sections, key=lambda item: -item.priority):
        text = (section.text or "").strip("\n")
        if not text:
            continue
        room = budget - used
        if room <= 0:
            break
        if len(text) > room:
            if kept:
                break
            text = text[:room]
        kept.append(text)
        used += len(text)
    return "\n".join(kept) + ("\n" if kept else "")


def compose_prompt(
    diary: DiaryLike,
    session: object,
    *,
    notebook: NotebookLike | None = None,
    state: StateLike | None = None,
    affinity: AffinityLike | None = None,
    now: object = None,
    strong_text: str = "",
    exchange_who: str = "",
    budget: int = PROMPT_BUDGET,
) -> str:
    """当前全部注入内容：小本本（约定 / 事实）+ 互动轨迹 + 状态（作息 / 情绪）+ 日记段。

    不回归保险丝：``state=None`` 且 ``affinity=None`` 时，输出与第 2 步逐字节相同
    （PRIORITY 里旧键的数值不动，新增的只有 ``trace``；有测试钉住）。
    熟悉度档位词并进 fact 档（§2.6 的表：100 字配额含档位词）；
    state 与 affinity 各自的渲染规则留在 ``core/state/`` 内部。
    """
    sections: list[Section] = []
    fact_text = ""
    if notebook is not None:
        promise = notebook.promise_line(session, now=now)
        if promise:
            sections.append(Section("promise", promise, PRIORITY["promise"]))
        fact_text = notebook.fact_line(session, now=now)
    band = affinity.line(session, now=now) if affinity is not None else ""
    fact_parts = [text for text in (fact_text, band) if text]
    if fact_parts:
        sections.append(Section("fact", "\n".join(fact_parts), PRIORITY["fact"]))
    if affinity is not None:
        trace = affinity.trace_line(session, now=now)
        if trace:
            sections.append(Section("trace", trace, PRIORITY["trace"]))
    if state is not None:
        parts = [
            text
            for text in (state.rhythm_line(now=now), state.mood_line(session, now=now))
            if text
        ]
        if parts:
            sections.append(Section("state", "\n".join(parts), PRIORITY["state"]))
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
    "NotebookLike",
    "Section",
    "StateLike",
    "compose_prompt",
    "render",
]
