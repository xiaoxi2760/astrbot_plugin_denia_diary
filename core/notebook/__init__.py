"""小本本子包：文件层（``store``）+ 语义入口（``api``）。

两类条目（方案 §4.2）：**约定型**（内容 / 关于谁 / 创建时间 / 期限 / 是否完成）
与**事实型**（关于谁 / 内容）。只记重要的："日常闲聊、情绪、玩笑不记"。
"""

from .api import Notebook
from .store import NotebookStore

__all__ = ["Notebook", "NotebookStore"]
