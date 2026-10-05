"""出站文本清洗（第 5.1 步）：把表情包标记从**发出去的消息**里剥掉。

**为什么需要它**（根因见交接文档 §四 2026-10-06 00:20，源码已实证）：

1. ``send_message_to_user`` 的 ``messages`` 只认 ``plain / image / record / video /
   file / mention_user`` 六种 type（``astrbot/core/tools/message_tools.py:77`` 起），
   **没有表情包类型**——``plain.text`` 里带的 ``&&sleep&&`` 会原样发出去；
2. 表情包插件唯一的处理点在 ``on_decorating_result``，而它第一行就是
   ``event.get_result()``，**直发路径没有 result 对象 → 拦不到**；
3. ``on_using_llm_tool`` 拿到的 ``tool_args`` 紧接着就被 ``execute(**valid_params)``
   展开成**同一个 dict** → 在钩子里改，真的作用于这次发送。

所以本模块只负责**纯计算**：吃什么、吐什么，不 import astrbot、不碰 HTTP、不读配置
（配置由 ``core.settings`` 校验后传进来）。真正的原地赋值在 ``main.py`` 的钩子里。

**两条设计红线**：

- **只剥无歧义的** ``&&名字&&``。meme_manager 的五种标记形态里只有这一种足够独特；
  ``[x]`` / ``(x)`` / ``:x:`` / ``meme:id`` **一律不动**——``(x)`` 会吃掉正常括号动作、
  ``:x:`` 会吃掉颜文字。**宁可漏，不可误伤**：误伤的代价是她说话变得莫名其妙。
- **剥完为空就不写回**（保持原文）。发一条空消息比发一条带标记的消息更糟。
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_MARKER_PATTERN = r"&&[^&\s]{1,24}&&"
"""默认标记正则：``&&`` 包住 1~24 个非 ``&`` 非空白字符。

上下界很重要——上界防住 ``&&&&`` 这种退化写法被当成两个标记，下界保证 ``&&&&``
这种空名字不匹配（它没有任何表情包含义，只会误伤正常文本）。
"""

OUTBOUND_TOOL = "send_message_to_user"
"""唯一允许清洗的工具名。其它工具、其它调用路径一个字节都不动。"""

PLAIN_TYPE = "plain"
"""唯一允许改 ``text`` 的组件类型。``image`` / ``record`` / ``video`` / ``file`` /
``mention_user`` 的字段一律不碰——改坏它们等于让消息发不出去。"""

_HORIZONTAL_SPACE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_NEWLINE = re.compile(r"[ \t]+(?=\n)")


def compile_marker(pattern: str = DEFAULT_MARKER_PATTERN) -> re.Pattern[str]:
    """编译标记正则，**任何坏输入都回落默认值，绝不抛异常**。

    三层防守：空/非字符串 → 默认；``re.error`` → 默认；能匹配空串（零宽）→ 默认
    （否则会把整条消息清空，那是灾难而不是 bug 修复）。
    """
    text = pattern if isinstance(pattern, str) else ""
    text = text.strip()
    if not text:
        return re.compile(DEFAULT_MARKER_PATTERN)
    try:
        compiled = re.compile(text)
    except re.error:
        return re.compile(DEFAULT_MARKER_PATTERN)
    if compiled.search("") is not None:  # 零宽模式会把整条文本吃掉
        return re.compile(DEFAULT_MARKER_PATTERN)
    return compiled


def strip_meme_marks(text: str, *, pattern: str = DEFAULT_MARKER_PATTERN) -> str:
    """剥掉一段文本里的标记，**并压掉因此产生的多余空格**。

    ``"……晚安，快睡吧。&&sleep&&"`` → ``"……晚安，快睡吧。"``
    ``"&&a&& 你好"`` → ``"你好"``
    ``"他说 &&x&& 好"`` → ``"他说 好"``（两个空格压成一个，不留悬空空格）
    ``"&&a&&&&b&&"`` → **原样返回**（剥完只剩空白 → 不写回，别发空串）

    换行**不合并**：``\\n`` 是消息里的正常结构，只压连续的水平空白。
    """
    if not isinstance(text, str) or not text:
        return text
    cleaned = compile_marker(pattern).sub("", text)
    cleaned = _HORIZONTAL_SPACE.sub(" ", cleaned)
    cleaned = _SPACE_BEFORE_NEWLINE.sub("", cleaned)  # 别在换行前留下悬空空格
    cleaned = cleaned.strip()
    if not cleaned:
        return text  # 剥空了就保持原文——绝不把空消息发出去
    return cleaned


def _clean_component(item: Any, matcher: re.Pattern[str]) -> Any:
    """只清洗一个 ``plain`` 组件的 ``text``；别的形态、别的类型原样返回。

    组件既可能是 LLM 传来的 dict（``{"type": "plain", "text": "..."}``），
    也可能是 astrbot 的组件对象——两种都支持，认不出来就原样返回。
    """
    if isinstance(item, dict):
        if item.get("type") != PLAIN_TYPE:
            return item
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return item
        cleaned = strip_meme_marks(text, pattern=matcher.pattern)
        if cleaned == text:
            return item
        return {**item, "text": cleaned}  # 复制后改，不动调用方手里的原组件
    if isinstance(item, str):
        # 裸字符串组件：只认看起来像 plain 的才动，其余（图片 base64 等）绝不碰
        if not item.strip():
            return item
        return strip_meme_marks(item, pattern=matcher.pattern)
    type_name = getattr(item, "type", None)
    if type_name != PLAIN_TYPE:
        return item
    text = getattr(item, "text", None)
    if not isinstance(text, str) or not text:
        return item
    cleaned = strip_meme_marks(text, pattern=matcher.pattern)
    if cleaned == text:
        return item
    try:
        copy_item = copy_component(item)
    except Exception:  # noqa: BLE001 - 拷不动就保持原样，宁可不剥也不弄坏消息
        return item
    try:
        setattr(copy_item, "text", cleaned)
    except Exception:  # noqa: BLE001 - 改不动就保持原样
        return item
    return copy_item


def copy_component(item: Any) -> Any:
    """浅拷贝一个组件对象（优先用它自己的 ``model_copy`` / ``copy``，都没有就 copy.copy）。"""
    for name in ("model_copy", "copy"):
        method = getattr(item, name, None)
        if callable(method):
            try:
                return method()
            except TypeError:
                continue
    import copy as _copy

    return _copy.copy(item)


def clean_messages(messages: Any, *, pattern: str = DEFAULT_MARKER_PATTERN) -> Any:
    """清洗 ``messages`` 列表里所有 ``plain`` 组件的文本。

    **纯函数：不改传入的对象**——没有变化时**原样返回同一个列表对象**（调用方可以靠
    ``cleaned is messages`` 判断"要不要写回"）。形状不对（非列表）时也原样返回。
    """
    if not isinstance(messages, list) or not messages:
        return messages
    matcher = compile_marker(pattern)
    out: list[Any] = []
    changed = False
    for item in messages:
        cleaned = _clean_component(item, matcher)
        if cleaned is not item:
            changed = True
        out.append(cleaned)
    return out if changed else messages


def has_meme_mark(text: str, *, pattern: str = DEFAULT_MARKER_PATTERN) -> bool:
    """文本里有没有待剥的标记（自检与测试用；不参与发送路径）。"""
    if not isinstance(text, str) or not text:
        return False
    return compile_marker(pattern).search(text) is not None


__all__ = [
    "DEFAULT_MARKER_PATTERN",
    "OUTBOUND_TOOL",
    "PLAIN_TYPE",
    "clean_messages",
    "compile_marker",
    "copy_component",
    "has_meme_mark",
    "strip_meme_marks",
]
