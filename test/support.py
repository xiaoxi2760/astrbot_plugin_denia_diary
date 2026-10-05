"""测试公共设施（文件名不匹配 ``test*.py``，不会被 discover 收集）。

两个环境注意点（都实测踩过）：

1. 不写系统临时目录——受限环境下系统 TEMP 可能不允许建嵌套目录；
2. 不用 ``mkdtemp`` / ``TemporaryDirectory``——某些沙箱下它们建出的目录**自身权限异常**
   （在其内部再建子目录会 WinError 5），改成显式 ``makedirs``。

可用环境变量 ``DIARY_TEST_TMP`` 覆盖临时根目录。
"""

from __future__ import annotations

import itertools
import os
import shutil
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core.diary.api import Diary  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402
from core.session import Session  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 4, 21, 30, tzinfo=TZ)

TMP_ROOT = Path(os.environ.get("DIARY_TEST_TMP") or (_HERE / ".tmp"))
_created = itertools.count()


class TmpDirCase(unittest.TestCase):
    """每个用例一个独立临时目录（在 ``test/.tmp`` 下，已 gitignore）。"""

    def setUp(self) -> None:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TMP_ROOT / f"{type(self).__name__.lower()}-{next(_created)}"
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


def make_diary(root: Path, config: dict | None = None) -> Diary:
    """造一个完整的日记门面（配置 → 布局 → 文件层 → 门面）。"""
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(config or {})
    limits = dict(raw.get("diary") or {})
    raw["diary"] = limits
    resolved = settings_mod.load_settings(raw)
    layout = storage.Layout(root).ensure()
    locks = storage.KeyedLocks()
    return Diary(
        settings=resolved,
        layout=layout,
        store=DiaryStore(layout=layout, locks=locks),
        locks=locks,
    )


def private_session(
    peer: str = "10001", name: str = "某某", umo: str | None = None
) -> Session:
    return Session(
        umo=umo or f"aiocqhttp:FriendMessage:{peer}",
        platform="aiocqhttp",
        message_type="FriendMessage",
        session_id=peer,
        sender_id=peer,
        sender_name=name,
    )


def group_session(
    group_id: str = "555",
    name: str = "某某群",
    sender: str = "10001",
    sender_name: str = "某某",
    umo: str | None = None,
) -> Session:
    return Session(
        umo=umo or f"aiocqhttp:GroupMessage:{group_id}",
        platform="aiocqhttp",
        message_type="GroupMessage",
        session_id=group_id,
        group_id=group_id,
        sender_id=sender,
        sender_name=sender_name,
        group_name=name,
    )


__all__ = [
    "NOW",
    "TMP_ROOT",
    "TZ",
    "TmpDirCase",
    "group_session",
    "make_diary",
    "private_session",
]
