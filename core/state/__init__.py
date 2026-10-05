"""状态系统（第 3 步）：情绪 + 作息（``State``）与熟悉度（``Affinity``）。

冻结的 import 面：``from core.state import State, Affinity``。包内文件怎么切是
实现自由——现在情绪与作息在 ``api.py``、熟悉度在 ``affinity.py``、两张 JSON 的
读写在 ``store.py``。
"""

from .affinity import Affinity
from .api import State

__all__ = ["Affinity", "State"]
