"""会话身份测试（第 1 步）。

``Session`` 是"按人 / 按本"的索引：标注写给谁、恋爱日记分流、提醒冷却粒度、恋爱日记可见性。
``from_event`` 必须靠鸭子类型工作（core 不 import astrbot）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.diary import format as fmt  # noqa: E402
from core.session import Session  # noqa: E402


class _FakeSender:
    def __init__(self, user_id: str = "", nickname: str = "") -> None:
        self.user_id = user_id
        self.nickname = nickname


class _FakeGroup:
    def __init__(self, group_name: str = "") -> None:
        self.group_name = group_name


class _FakeMessage:
    def __init__(self, **kwargs) -> None:
        self.type = kwargs.get("type", "")
        self.session_id = kwargs.get("session_id", "")
        self.group_id = kwargs.get("group_id", "")
        self.sender = kwargs.get("sender")
        self.group = kwargs.get("group")


class _FakeEvent:
    """长得像 AstrMessageEvent 的最小替身（正是 ``from_event`` 依赖的形状）。"""

    def __init__(self, umo: str, **kwargs) -> None:
        self.unified_msg_origin = umo
        self.message_obj = _FakeMessage(**kwargs)
        self._group_id = kwargs.get("group_id", "")
        self._sender_id = getattr(kwargs.get("sender"), "user_id", "")
        self._sender_name = getattr(kwargs.get("sender"), "nickname", "")

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name


class _AngryEvent(_FakeEvent):
    """平台实现异常时不许把整条链带崩。"""

    def get_group_id(self) -> str:
        raise RuntimeError("platform exploded")

    def get_sender_id(self) -> str:
        raise RuntimeError("platform exploded")


class SessionTest(unittest.TestCase):
    def test_from_umo_splits_three_parts(self) -> None:
        session = Session.from_umo("aiocqhttp:GroupMessage:555")
        self.assertEqual(session.platform, "aiocqhttp")
        self.assertEqual(session.message_type, "GroupMessage")
        self.assertEqual(session.session_id, "555")
        self.assertTrue(session.is_group)

    def test_from_umo_tolerates_missing_parts(self) -> None:
        session = Session.from_umo("")
        self.assertEqual(session.umo, "")
        self.assertEqual(session.kind, "private")

    def test_from_event_reads_everything(self) -> None:
        event = _FakeEvent(
            "aiocqhttp:GroupMessage:555",
            group_id="555",
            sender=_FakeSender("10001", "某某"),
            group=_FakeGroup("某某群"),
        )
        session = Session.from_event(event)
        self.assertEqual(session.group_id, "555")
        self.assertEqual(session.sender_id, "10001")
        self.assertEqual(session.sender_name, "某某")
        self.assertEqual(session.group_name, "某某群")
        self.assertTrue(session.is_group)
        self.assertEqual(session.kind, "group")

    def test_from_event_private_has_no_group(self) -> None:
        event = _FakeEvent(
            "aiocqhttp:FriendMessage:10001",
            sender=_FakeSender("10001", "某某"),
        )
        session = Session.from_event(event)
        self.assertTrue(session.is_private)
        self.assertEqual(session.peer_id(), "10001")

    def test_from_event_degrades_when_platform_raises(self) -> None:
        """平台 getter 抛异常时不许崩，退回 ``message_obj`` 里的字段。"""
        event = _AngryEvent(
            "aiocqhttp:GroupMessage:555",
            group_id="555",
            sender=_FakeSender("10001", "某某"),
            group=_FakeGroup("某某群"),
        )
        session = Session.from_event(event)
        self.assertEqual(session.umo, "aiocqhttp:GroupMessage:555")
        self.assertEqual(session.group_id, "555")  # 来自 message_obj
        self.assertEqual(session.sender_id, "10001")
        self.assertEqual(session.group_name, "某某群")

    def test_is_group_falls_back_to_group_id(self) -> None:
        session = Session(umo="x:y:z", group_id="555")
        self.assertTrue(session.is_group)
        self.assertEqual(session.kind, "group")

    def test_private_label_uses_name_then_preference(self) -> None:
        session = Session.from_umo("aiocqhttp:FriendMessage:10001")
        session = Session(**{**session.__dict__, "sender_id": "10001", "sender_name": "某某"})
        self.assertEqual(session.label(), "和某某")
        self.assertEqual(session.label({"10001": "特别的称呼"}), "和特别的称呼")

    def test_private_label_falls_back_to_id(self) -> None:
        session = Session(umo="aiocqhttp:FriendMessage:10001", sender_id="10001")
        self.assertEqual(session.label(), "和10001")

    def test_private_label_ignores_synthetic_sender_name(self) -> None:
        """回归（2026-10-05 真机）：主动轮/定时唤醒没有 sender_id，平台塞的名字不是人名。

        旧写法优先信 ``sender_name``，于是 10:00 那段日记落成了 ``〔和Scheduler〕``。
        """
        session = Session(
            umo="aiocqhttp:FriendMessage:508416913",
            message_type="FriendMessage",
            session_id="508416913",
            sender_name="Scheduler",
        )
        self.assertEqual(session.label(), "和508416913")
        self.assertEqual(session.label({"508416913": "希"}), "和希")
        self.assertEqual(
            session.label({"Scheduler": "希"}), "和508416913", "合成名字不是索引键"
        )

    def test_cron_wake_sender_is_synthetic(self) -> None:
        """回归（真机 2026-10-05 10:00）：cron 唤醒的 sender 是合成的。

        上游 ``CronMessageEvent`` 把 ``sender.user_id`` 设成 ``session.session_id``、
        ``nickname`` 固定成 ``"Scheduler"`` —— 所以"id 为空才不信名字"这个条件
        永远不成立，头行照样落成 ``〔和Scheduler〕``。
        """
        event = _FakeEvent(
            "aiocqhttp:FriendMessage:508416913",
            sender=_FakeSender("508416913", "Scheduler"),
        )
        event.get_extra = lambda key=None, default=None: (
            {"id": "job1"} if key == "cron_job" else default
        )
        session = Session.from_event(event)
        self.assertEqual(session.sender_id, "508416913")
        self.assertEqual(session.sender_name, "", "合成昵称必须丢掉")
        self.assertEqual(session.person_id(), "508416913")
        self.assertEqual(session.label(), "和508416913")
        self.assertEqual(session.label({"508416913": "希"}), "和希")

    def test_cron_name_fallback_without_cron_extra(self) -> None:
        """上游万一改 extra 字段名：靠「昵称固定 + id 等于会话 id」兜底。

        误判的代价只是标注退回 id（真有个群友叫 Scheduler 也一样），不会写错人。
        """
        event = _FakeEvent(
            "aiocqhttp:FriendMessage:10001", sender=_FakeSender("10001", "Scheduler")
        )
        self.assertEqual(Session.from_event(event).label(), "和10001")

    def test_group_label_carries_explicit_scope_prefix(self) -> None:
        session = Session(
            umo="aiocqhttp:GroupMessage:555", group_id="555", group_name="和平精英交流群"
        )
        label = session.label()
        self.assertEqual(label, f"{fmt.GROUP_TAG_PREFIX}和平精英交流群")
        self.assertEqual(fmt.scope_of(label), "group", "带前缀后不再被『和』字开头的启发式误判")

    def test_group_label_falls_back_to_group_id(self) -> None:
        session = Session(umo="aiocqhttp:GroupMessage:555", group_id="555")
        self.assertEqual(session.label(), "群·群555")

    def test_label_empty_when_nothing_known(self) -> None:
        self.assertEqual(Session().label(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
