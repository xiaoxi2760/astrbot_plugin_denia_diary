"""图像提示词构建（任务 2 第 2 条）：**离线可测的纯函数**，把「她当前在做什么、在哪、
心情怎样 + 人物形象锚定」拼成一条生图提示词。

三条硬边界：

1. **数据只来自公开层**——调用方只许喂 ``Life.snapshot`` 的当前活动、``State.snapshot``
   的作息词 / 情绪词和主人手填的锚定文案。私聊日记、恋爱本、小本本、主人私人记忆
   都不在参数里，想泄也泄不进去（``test_imagery_prompt`` 用私密字符串钉住这一条）；
2. **提示词不进聊天注入**——本模块只被 ``core.imagery.api`` 调用，``core.compose``
   的 400 字预算与它无关；
3. **确定性**——同样输入必得同样输出（缓存键依赖这一点），不掷骰子、不取当前时间。

只依赖标准库；不 import astrbot。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PROMPT_MAX = 700
"""整条提示词的长度上限。SD 系模型的文本编码窗口有限，超长只会被服务端截烂；
各分段在上游已各自限长（character_hint 300 / style_hint 200 / 活动 title·location 60），
这里只兜整体的总闸。"""

MOOD_MAX = 24
"""情绪词进提示词的上限：她自报的原词可能很长，截短即可，别让它吃掉整条预算。"""

DEFAULT_NEGATIVE_PROMPT = "低质量, 模糊, 变形, 多余的手指, 水印, 文字, 签名"
"""固定负向提示词：只服务支持 negative 概念的提供方（A1111）；OpenAI 兼容 Images
没有这个概念，适配层不会把它发出去。不开放配置——保持首版行为可预期。"""

FALLBACK_PROMPT = "一个安静放松的日常瞬间"
"""连作息词都没有时的兜底句：生图仍然可用，不因为生活日程没生成而拒绝。"""


def build_scene(spec: Mapping[str, Any]) -> dict[str, str]:
    """公开日常要素 → ``{"prompt", "negative_prompt"}``。

    ``spec`` 键（全部可选，缺键按空串处理）：

    - ``character_hint``：人物形象锚定（主人手填，最多 300 字）；
    - ``style_hint``：画风锚定（最多 200 字）；
    - ``activity``：``Life.snapshot`` 的当前活动 ``{"title", "location"}``，没有传 ``None``；
    - ``rhythm_word``：``State.snapshot`` 的作息状态词（如「晚饭后放松」）；
    - ``mood_word``：她自报的情绪原词（如「有点开心」）。

    句序固定：人物 → 正在做什么 → 情绪 → 画风 → 兜底。**不出现**日记正文、
    对话记录或任何用户隐私字段——参数里根本没有它们。
    """

    def text(key: str) -> str:
        value = spec.get(key)
        return str(value).strip() if isinstance(value, str) else ""

    character = text("character_hint")
    style = text("style_hint")
    mood = text("mood_word")[:MOOD_MAX]
    rhythm = text("rhythm_word")
    activity = spec.get("activity") if isinstance(spec.get("activity"), Mapping) else None
    title = str(activity.get("title") or "").strip() if activity else ""
    location = str(activity.get("location") or "").strip() if activity else ""

    parts: list[str] = []
    if character:
        parts.append(f"画中的她：{character}。")
    if title:
        doing = f"她正在{title}"
        doing += f"（在{location}）" if location else ""
        parts.append(doing + "。")
    elif rhythm:
        parts.append(f"她现在{rhythm}。")
    if mood:
        parts.append(f"此刻的心情是「{mood}」。")
    if style:
        parts.append(f"画面风格：{style}。")
    if not parts:
        parts.append(FALLBACK_PROMPT + "。")
    prompt = "".join(parts).strip()
    if len(prompt) > PROMPT_MAX:
        prompt = prompt[:PROMPT_MAX].rstrip()
    return {"prompt": prompt, "negative_prompt": DEFAULT_NEGATIVE_PROMPT}


__all__ = [
    "DEFAULT_NEGATIVE_PROMPT",
    "FALLBACK_PROMPT",
    "MOOD_MAX",
    "PROMPT_MAX",
    "build_scene",
]
