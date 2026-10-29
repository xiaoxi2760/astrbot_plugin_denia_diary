"""语音出口的决策层（F1 第一步）：**转不转、转哪些、怎么换**——全是纯函数，零 IO。

分工（app-ready 横切原则）：这里只做决策与消息变换，**合成**（调宿主 TTS provider、
拿到音频文件路径）是适配层的事——S1 壳将来接自己的 TTS 时复用同一套决策。

上游事实（DEVELOPMENT.md「语音」一节有出处）：宿主 ``TTSProvider.get_audio(text)``
**只有文本参数**（音色/语速全在 provider 配置里），所以第一步情绪只能影响「开不开口」
（valence<0 概率上浮），影响不了「什么声音」——音色路由要自带客户端（tts_emotion_router
的做法），挂待拍板。

红线：语音坏了不能连累她说话——所有函数对坏输入都退回"保持原样"。
"""

from __future__ import annotations

from typing import Any

VOICE_MODES: tuple[str, ...] = ("off", "voice", "dual")
"""off=不发语音（默认）；voice=语音替换文字；dual=语音在前、文字在后（对齐宿主
``dual_output`` 的顺序惯例）。"""

VOICE_MODE_DEFAULT = "off"

MAX_SPEAK_CHARS = 200
"""单个文本组件的开口上限（待调）：太长读出来很怪，超长保持文字。"""

SOFT_VALENCE_BOOST = 0.3
"""情绪倾向（待调）：当下 valence < 0 时语音概率上浮的绝对量（封顶 100%）——
情绪不好时更倾向开口，声音比文字近。"""

_PLAIN_TYPES = ("plain",)


def speak_plan(
    *,
    mode: str,
    probability: int,
    valence: float | int | None = None,
    roll: float = 1.0,
) -> bool:
    """这条主动消息要不要开口（适配层拿到 True 才去调 provider 合成）。

    ``roll`` 是调用方掷好的随机数（0~1，测试注入用）——决策保持纯函数、可复现；
    ``valence`` 是衰减后的当下坐标（``state.snapshot()``），没自报过就是 None（不加成）。
    边界语义：probability ≤ 0 永不开口、≥ 100 必开口，与 roll 无关。
    """
    if str(mode or "") not in ("voice", "dual"):
        return False
    try:
        p = int(probability) / 100
    except (TypeError, ValueError):
        p = 1.0
    p = max(0.0, min(1.0, p))
    if valence is not None:
        try:
            if float(valence) < 0:
                p = min(1.0, p + SOFT_VALENCE_BOOST)
        except (TypeError, ValueError):
            pass
    if p <= 0.0:
        return False
    if p >= 1.0:
        return True
    return float(roll) < p


def speakable_indices(messages: Any) -> list[int]:
    """值得开口的 plain 组件下标：非空、纯文本、不超长。坏输入返回空表。"""
    if not isinstance(messages, list):
        return []
    out: list[int] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() not in _PLAIN_TYPES:
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > MAX_SPEAK_CHARS:
            continue
        out.append(index)
    return out


def apply_audio(
    messages: list[Any], audio: dict[int, str], *, mode: str, url_mode: bool = False
) -> list[Any] | None:
    """把合成好的音频（``{plain 下标: 音频路径或 URL}``）变换进消息列表。

    - ``voice`` 档：plain **替换**为 ``{"type": "record", "path": ...}``；
    - ``dual`` 档：plain 保留，record 插在它**后面**（宿主 dual_output 同序）；
    - ``url_mode=True``：record 改带 ``url`` 键——宿主文件服务的交接契约
      （``callback_api_base/api/file/<token>``，协议端与宿主不同文件系统时唯一能通的形）；
    - **没有任何一处成功 → 返回 None**（调用方保持原对象，不发半截语音）；
    - 非 plain 组件一个字节不碰。

    返回**新列表**（不原地改——调用方决定要不要写回 ``tool_args``）。
    """
    if str(mode or "") not in ("voice", "dual") or not audio:
        return None
    out: list[Any] = []
    changed = False
    for index, item in enumerate(messages):
        path = audio.get(index)
        is_plain = (
            isinstance(item, dict)
            and str(item.get("type") or "").lower() in _PLAIN_TYPES
        )
        if not is_plain or not path:
            out.append(item)
            continue
        record: dict[str, str] = {"type": "record"}
        record["url" if url_mode else "path"] = str(path)
        if mode == "dual":
            out.append(item)
            out.append(record)
        else:
            out.append(record)
        changed = True
    return out if changed else None


__all__ = [
    "MAX_SPEAK_CHARS",
    "SOFT_VALENCE_BOOST",
    "VOICE_MODES",
    "VOICE_MODE_DEFAULT",
    "apply_audio",
    "speak_plan",
    "speakable_indices",
]
