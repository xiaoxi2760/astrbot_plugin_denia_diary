"""语音出口（F1 第一步 + F2 收听 + F3 产物 + F4 能力）：她说文字，宿主 TTS 发声；他发语音，宿主转写。

对外只暴露四块，全部**零 astrbot 依赖**：

- ``plan``：决策纯函数——``speak_plan`` / ``speakable_indices`` / ``apply_audio``。
- ``speak``：合成的有界执行（超时 / 总预算 / 分段上限 / 取消边界）。
- ``listen``：被动收听——宿主转写优先、兜底去重判定（不造第二份 ASR）。
- ``artifacts``：音频产物台账（引用保底、保留期、清理名单）。
- ``status``：能力绑定报告与诊断事件环（面板只说查证过的事实）。

合成 / 转写本身是适配层的事（宿主 provider ``get_audio(text)`` / ``get_text(audio_url)``），
core 不碰 IO（speak/listen 的 provider 由调用方注入）。
"""

from . import artifacts, listen, plan, speak, status
from .plan import (
    MAX_SPEAK_CHARS,
    SOFT_VALENCE_BOOST,
    VOICE_MODES,
    VOICE_MODE_DEFAULT,
    apply_audio,
    speak_plan,
    speakable_indices,
)

__all__ = [
    "MAX_SPEAK_CHARS",
    "SOFT_VALENCE_BOOST",
    "VOICE_MODES",
    "VOICE_MODE_DEFAULT",
    "apply_audio",
    "artifacts",
    "listen",
    "plan",
    "speak",
    "speak_plan",
    "speakable_indices",
    "status",
]
