"""早晚安记录（用户发起面）：他说早安/晚安 → 记下来 → 她自然接话。

对外只暴露门面与文件层：``Greeting`` / ``GreetingStore``。识别是整条消息的
正则完全匹配（``classify``），连续天数与睡眠时长是派生值不落盘；与主动消息
的双向防重由调用方在 ``observe`` 命中后调 ``Proactive.mark_slot_today`` 完成。
"""

from .api import Greeting
from .store import GreetingStore

__all__ = ["Greeting", "GreetingStore"]
