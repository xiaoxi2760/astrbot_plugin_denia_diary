"""语音出口（F1 第一步）：她说文字，宿主 TTS 发声。

对外只暴露决策纯函数（``plan``）：``speak_plan`` / ``speakable_indices`` / ``apply_audio``。
合成本身是适配层的事（宿主 provider ``get_audio(text)``），core 不碰 IO。
"""

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
    "speak_plan",
    "speakable_indices",
]
