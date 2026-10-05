"""日记子包：格式契约（``format``）+ 文件层（``store``）+ 语义入口（``api``）。"""

from .api import Diary
from .format import Entry, Segment
from .store import DiaryStore

__all__ = ["Diary", "DiaryStore", "Entry", "Segment"]
