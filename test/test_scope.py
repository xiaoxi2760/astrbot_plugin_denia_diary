"""core/scope（第 7 步）的离线单测：三档判定表 + 主动目标集合 + 配置降级。

三档语义是用户拍板（别改语义）：

1. 「主人」= ``love_peers``；
2. ``all`` 档允许主动消息进群；
3. 默认档 = ``private``。

纯函数、零 astrbot——不需要起宿主就能全量验收。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core import scope  # noqa: E402
from core import settings as settings_mod  # noqa: E402

GROUP_UMO = "aiocqhttp:GroupMessage:555"
MASTER_PRIVATE = "aiocqhttp:FriendMessage:10001"
OTHER_PRIVATE = "aiocqhttp:FriendMessage:20002"

CONTACTS = {
    "10001": {"umo": MASTER_PRIVATE, "kind": "private"},
    "20002": {"umo": OTHER_PRIVATE, "kind": "private"},
    "30003": {"umo": GROUP_UMO, "kind": "group"},
}
LOVE_PEERS = ("10001",)


class NormalizeModeTest(unittest.TestCase):
    def test_known_values_pass_through(self) -> None:
        for mode in scope.MODES:
            self.assertEqual(scope.normalize_mode(mode), (mode, ""))

    def test_case_and_whitespace_are_tolerated(self) -> None:
        self.assertEqual(scope.normalize_mode(" ALL "), ("all", ""))
        self.assertEqual(scope.normalize_mode("Private"), ("private", ""))

    def test_unset_is_silently_default(self) -> None:
        self.assertEqual(scope.normalize_mode(None), (scope.DEFAULT_MODE, ""))

    def test_unknown_value_falls_back_with_warning(self) -> None:
        for raw in ("yolo", "", 3, ["all"]):
            mode, warning = scope.normalize_mode(raw)
            self.assertEqual(mode, scope.DEFAULT_MODE)
            self.assertTrue(warning, f"{raw!r} 必须给一句人话原因")
            self.assertIn("scope.mode", warning)

    def test_mode_constants(self) -> None:
        self.assertEqual(scope.DEFAULT_MODE, "private")
        self.assertEqual(scope.MODES, ("owner", "private", "all"))


class AllowsTest(unittest.TestCase):
    """三档 × {私聊主人, 私聊别人, 群聊} 的 3×3 判定表，逐格断言。"""

    def test_three_by_three_table(self) -> None:
        cases = [
            # (mode,     私聊?, 人,     期望放行, 拒绝原因须含)
            ("owner", True, "10001", True, ""),
            ("owner", True, "20002", False, "只主人"),
            ("owner", False, "10001", False, "群聊不启用"),
            ("private", True, "10001", True, ""),
            ("private", True, "20002", True, ""),
            ("private", False, "10001", False, "群聊不启用"),
            ("all", True, "10001", True, ""),
            ("all", True, "20002", True, ""),
            ("all", False, "10001", True, ""),
        ]
        for mode, is_private, person, expected, reason_part in cases:
            with self.subTest(mode=mode, is_private=is_private, person=person):
                allowed, reason = scope.allows(
                    mode, is_private=is_private, person_id=person, love_peers=LOVE_PEERS
                )
                self.assertEqual(allowed, expected, f"{mode} × {'私聊' if is_private else '群聊'}")
                if expected:
                    self.assertEqual(reason, "")
                else:
                    self.assertTrue(reason, "拒绝必须给一句人话")
                    self.assertIn(reason_part, reason)

    def test_unknown_mode_is_defensively_private(self) -> None:
        # 调用方忘了先 normalize_mode 也不放行群聊（fail-closed）
        allowed, _ = scope.allows("乱写的", is_private=False, person_id="10001", love_peers=LOVE_PEERS)
        self.assertFalse(allowed)

    def test_empty_love_peers_blocks_owner_private(self) -> None:
        allowed, _ = scope.allows("owner", is_private=True, person_id="10001", love_peers=())
        self.assertFalse(allowed)


class ProactiveTargetsTest(unittest.TestCase):
    def test_owner_targets_only_master_private(self) -> None:
        targets = scope.proactive_targets(
            "owner", contacts=CONTACTS, love_peers=LOVE_PEERS,
            sessions={}, known_groups=[GROUP_UMO],
        )
        self.assertEqual(targets, [(MASTER_PRIVATE, "private")])

    def test_private_targets_all_interacted_private_chats(self) -> None:
        targets = scope.proactive_targets(
            "private", contacts=CONTACTS, love_peers=LOVE_PEERS,
            sessions={}, known_groups=[GROUP_UMO],
        )
        self.assertEqual(targets, [(MASTER_PRIVATE, "private"), (OTHER_PRIVATE, "private")])

    def test_all_targets_add_interacted_groups(self) -> None:
        targets = scope.proactive_targets(
            "all", contacts=CONTACTS, love_peers=LOVE_PEERS,
            sessions={}, known_groups=[GROUP_UMO],
        )
        self.assertEqual(
            targets,
            [(MASTER_PRIVATE, "private"), (OTHER_PRIVATE, "private"), (GROUP_UMO, "group")],
        )

    def test_never_interacted_person_never_enters(self) -> None:
        """99999 在名单里但从没互动过（contacts 无条目＝没有 umo）——任何档位都进不来。"""
        for mode in scope.MODES:
            with self.subTest(mode=mode):
                targets = scope.proactive_targets(
                    mode, contacts=CONTACTS, love_peers=("10001", "99999"),
                    sessions={}, known_groups=[GROUP_UMO],
                )
                umos = [umo for umo, _ in targets]
                self.assertNotIn("", umos, "不该出现空 umo 的幽灵目标")
                self.assertTrue(
                    set(umos) <= {MASTER_PRIVATE, OTHER_PRIVATE, GROUP_UMO},
                    f"目标只能来自 contacts 里真实互动过的会话：{umos}",
                )

    def test_unseen_group_never_enters(self) -> None:
        """known_groups 为空＝她没在群里活动过，all 档也不许凭空构造群目标。"""
        targets = scope.proactive_targets(
            "all", contacts=CONTACTS, love_peers=LOVE_PEERS, sessions={}, known_groups=[],
        )
        self.assertNotIn(GROUP_UMO, [umo for umo, _ in targets])

    def test_contacts_group_entries_alone_do_not_become_targets(self) -> None:
        """群目标必须走 known_groups（调用方从 contacts 归并传入）——contacts 里的
        group 条目自己不会变成目标；known_groups 为空就一个群都没有。"""
        targets = scope.proactive_targets(
            "all", contacts=CONTACTS, love_peers=LOVE_PEERS, sessions={}, known_groups=[],
        )
        self.assertNotIn(GROUP_UMO, [umo for umo, _ in targets])

    def test_group_targets_only_in_all_mode(self) -> None:
        for mode in ("owner", "private"):
            with self.subTest(mode=mode):
                targets = scope.proactive_targets(
                    mode, contacts=CONTACTS, love_peers=LOVE_PEERS,
                    sessions={}, known_groups=[GROUP_UMO],
                )
                self.assertNotIn(GROUP_UMO, [umo for umo, _ in targets])

    def test_groups_are_deduplicated(self) -> None:
        targets = scope.proactive_targets(
            "all", contacts=CONTACTS, love_peers=LOVE_PEERS,
            sessions={}, known_groups=[GROUP_UMO, GROUP_UMO],
        )
        self.assertEqual([umo for umo, _ in targets].count(GROUP_UMO), 1)

    def test_sessions_only_affects_ordering_not_membership(self) -> None:
        without = scope.proactive_targets(
            "private", contacts=CONTACTS, love_peers=LOVE_PEERS, sessions={}, known_groups=[],
        )
        with_state = scope.proactive_targets(
            "private", contacts=CONTACTS, love_peers=LOVE_PEERS,
            sessions={OTHER_PRIVATE: {"today_count": 1}}, known_groups=[],
        )
        self.assertEqual(set(without), set(with_state), "sessions 不扩大也不缩小目标集合")
        self.assertEqual(with_state[0], (OTHER_PRIVATE, "private"), "已有主动轨迹的会话排前")

    def test_broken_contact_entries_are_skipped(self) -> None:
        contacts = {
            "10001": "不是对象",
            "20002": {"umo": "", "kind": "private"},
            "30003": {"umo": GROUP_UMO, "kind": "奇怪的kind"},
        }
        targets = scope.proactive_targets(
            "all", contacts=contacts, love_peers=(), sessions={}, known_groups=[GROUP_UMO],
        )
        self.assertEqual(targets, [(GROUP_UMO, "group")])

    def test_owner_mode_order_follows_love_peers_order(self) -> None:
        contacts = {
            "20002": {"umo": OTHER_PRIVATE, "kind": "private"},
            "10001": {"umo": MASTER_PRIVATE, "kind": "private"},
        }
        targets = scope.proactive_targets(
            "owner", contacts=contacts, love_peers=("10001", "20002"), sessions={}, known_groups=[],
        )
        self.assertEqual(targets, [(MASTER_PRIVATE, "private"), (OTHER_PRIVATE, "private")])


class ScopeSettingsTest(unittest.TestCase):
    def test_default_mode_is_private(self) -> None:
        resolved = settings_mod.load_settings({})
        self.assertEqual(dict(resolved.scope), {"mode": "private"})

    def test_explicit_mode_round_trips(self) -> None:
        for mode in scope.MODES:
            resolved = settings_mod.load_settings({"scope": {"mode": mode}})
            self.assertEqual(dict(resolved.scope), {"mode": mode})
            self.assertEqual(resolved.warnings, ())

    def test_unknown_mode_falls_back_with_warning(self) -> None:
        resolved = settings_mod.load_settings({"scope": {"mode": "每天"}})
        self.assertEqual(dict(resolved.scope), {"mode": "private"})
        self.assertTrue(any("scope.mode" in item for item in resolved.warnings))

    def test_broken_scope_group_falls_back(self) -> None:
        resolved = settings_mod.load_settings({"scope": "不是对象"})
        self.assertEqual(dict(resolved.scope), {"mode": "private"})
        self.assertTrue(any("scope 不是对象" in item for item in resolved.warnings))
        resolved = settings_mod.load_settings({"scope": {"mode": ["all"]}})
        self.assertEqual(dict(resolved.scope), {"mode": "private"})

    def test_scope_is_declared_in_default_config(self) -> None:
        self.assertIn("scope", settings_mod.default_config())
        self.assertEqual(settings_mod.default_config()["scope"], {"mode": "private"})


if __name__ == "__main__":
    unittest.main()
