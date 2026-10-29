"""状态生图（任务 2）：门面 + 两个真实协议适配器 + 缓存 / 配额 / 提示词。"""

from .api import Imagery, capabilities_of
from .providers import ProviderError

__all__ = ["Imagery", "ProviderError", "capabilities_of"]
