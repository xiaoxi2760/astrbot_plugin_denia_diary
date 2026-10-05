"""``core.settings`` 的行为测试（第 0 步验收）。

覆盖：默认值 / 越界夹紧 / 非法回落并记 warning / 子系统开关 / 名单与称呼清洗 / 只读性。
"""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import settings as settings_mod  # noqa: E402


class DefaultsTest(unittest.TestCase):
    def test_none_config_is_all_defaults_without_warnings(self) -> None:
        result = settings_mod.load_settings(None)
        self.assertTrue(result.enabled)
        self.assertEqual(result.timezone, settings_mod.DEFAULT_TIMEZONE)
        self.assertEqual(result.data_dir, "")
        self.assertEqual(result.warnings, ())
        self.assertEqual(dict(result.diary), settings_mod.default_config()["diary"])

    def test_every_subsystem_defaults_on(self) -> None:
        result = settings_mod.load_settings({})
        for name in settings_mod.SUBSYSTEMS:
            self.assertTrue(result.subsystem(name))

    def test_default_config_is_a_fresh_object(self) -> None:
        first = settings_mod.default_config()
        first["diary"]["max_chars"] = 1
        self.assertNotEqual(first["diary"], settings_mod.default_config()["diary"])

    def test_zone_returns_zoneinfo(self) -> None:
        result = settings_mod.load_settings(None)
        self.assertIsInstance(result.zone(), ZoneInfo)
        self.assertEqual(str(result.zone()), settings_mod.DEFAULT_TIMEZONE)

    def test_settings_is_frozen(self) -> None:
        result = settings_mod.load_settings(None)
        with self.assertRaises(FrozenInstanceError):
            result.enabled = False  # type: ignore[misc]


class DegradationTest(unittest.TestCase):
    def _only_warning(self, raw: dict) -> str:
        result = settings_mod.load_settings(raw)
        self.assertEqual(len(result.warnings), 1, result.warnings)
        return result.warnings[0]

    def test_clamps_int_above_upper_bound(self) -> None:
        result = settings_mod.load_settings({"diary": {"edit_within_days": 999, "max_chars": 10**9}})
        self.assertEqual(result.diary["edit_within_days"], 60)
        self.assertEqual(result.diary["max_chars"], 20000)
        self.assertEqual(len(result.warnings), 2)

    def test_clamps_int_below_lower_bound(self) -> None:
        result = settings_mod.load_settings({"diary": {"edit_within_days": 0, "max_chars": -5}})
        self.assertEqual(result.diary["edit_within_days"], 1)
        self.assertEqual(result.diary["max_chars"], 200)

    def test_non_int_falls_back_to_default(self) -> None:
        result = settings_mod.load_settings({"diary": {"max_chars": "1200"}})
        self.assertEqual(result.diary["max_chars"], 1200)
        self.assertIn("不是整数", result.warnings[0])

    def test_bool_is_not_accepted_as_int(self) -> None:
        result = settings_mod.load_settings({"diary": {"max_chars": True}})
        self.assertEqual(result.diary["max_chars"], 1200)
        self.assertIn("不是整数", result.warnings[0])

    def test_unknown_keys_are_reported_and_ignored(self) -> None:
        warning = self._only_warning({"max_char": 5})
        self.assertIn("max_char", warning)

    def test_wrong_container_types_are_reported(self) -> None:
        result = settings_mod.load_settings({"diary": 5, "subsystems": "all", "love_peers": "1"})
        joined = " | ".join(result.warnings)
        self.assertIn("diary 不是对象", joined)
        self.assertIn("subsystems 不是对象", joined)
        self.assertIn("id 名单不是列表", joined)
        self.assertEqual(result.diary["max_chars"], 1200)

    def test_state_group_non_string_falls_back(self) -> None:
        result = settings_mod.load_settings({"state": {"rhythm": 5, "late_night": ["x"]}})
        joined = " | ".join(result.warnings)
        self.assertIn("state.rhythm", joined)
        self.assertIn("state.late_night", joined)
        self.assertEqual(result.state["rhythm"], settings_mod.default_config()["state"]["rhythm"])
        self.assertEqual(
            result.state["late_night"], settings_mod.default_config()["state"]["late_night"]
        )

    def test_state_group_wrong_container_uses_defaults(self) -> None:
        result = settings_mod.load_settings({"state": "nonsense"})
        self.assertIn("state 不是对象", result.warnings[0])
        self.assertEqual(dict(result.state), settings_mod.default_config()["state"])

    def test_state_group_values_are_stripped(self) -> None:
        result = settings_mod.load_settings({"state": {"late_night": " 01:00-05:00 "}})
        self.assertEqual(result.state["late_night"], "01:00-05:00")

    def test_non_mapping_config_falls_back_entirely(self) -> None:
        result = settings_mod.load_settings("nonsense")  # type: ignore[arg-type]
        self.assertTrue(result.enabled)
        self.assertIn("配置不是对象", result.warnings[0])

    def test_invalid_timezone_falls_back(self) -> None:
        result = settings_mod.load_settings({"timezone": "Mars/Olympus"})
        self.assertEqual(result.timezone, settings_mod.DEFAULT_TIMEZONE)
        self.assertIn("时区", result.warnings[0])

    def test_valid_timezone_is_kept(self) -> None:
        result = settings_mod.load_settings({"timezone": "UTC"})
        self.assertEqual(result.timezone, "UTC")
        self.assertEqual(result.warnings, ())

    def test_blank_timezone_uses_default_silently(self) -> None:
        result = settings_mod.load_settings({"timezone": "   "})
        self.assertEqual(result.timezone, settings_mod.DEFAULT_TIMEZONE)
        self.assertEqual(result.warnings, ())


class SwitchAndListTest(unittest.TestCase):
    def test_enabled_false_disables_every_subsystem(self) -> None:
        result = settings_mod.load_settings({"enabled": False})
        for name in settings_mod.SUBSYSTEMS:
            self.assertFalse(result.subsystem(name))

    def test_partial_subsystem_switch(self) -> None:
        result = settings_mod.load_settings({"subsystems": {"proactive": False}})
        self.assertFalse(result.subsystem("proactive"))
        self.assertTrue(result.subsystem("diary"))
        self.assertTrue(result.subsystem("notebook"))
        self.assertTrue(result.subsystem("state"))

    def test_non_bool_subsystem_falls_back_to_true(self) -> None:
        result = settings_mod.load_settings({"subsystems": {"diary": "no"}})
        self.assertTrue(result.subsystem("diary"))
        self.assertIn("subsystems.diary", result.warnings[0])

    def test_int_style_bool_is_accepted(self) -> None:
        result = settings_mod.load_settings({"enabled": 0, "subsystems": {"diary": 1}})
        self.assertFalse(result.enabled)
        self.assertTrue(result.subsystems["diary"])
        self.assertEqual(result.warnings, ())

    def test_love_peers_is_cleaned_deduped_and_ordered(self) -> None:
        result = settings_mod.load_settings({"love_peers": [" 10001 ", "", 10002, "10001", None]})
        self.assertEqual(result.love_peers, ("10001", "10002"))

    def test_name_preference_drops_empty_entries(self) -> None:
        result = settings_mod.load_settings(
            {"name_preference": {"10001": " 某某 ", "": "x", "20002": "", "30003": 42}}
        )
        self.assertEqual(dict(result.name_preference), {"10001": "某某", "30003": "42"})

    def test_name_preference_wrong_type_is_reported(self) -> None:
        result = settings_mod.load_settings({"name_preference": ["10001"]})
        self.assertEqual(dict(result.name_preference), {})
        self.assertIn("name_preference 不是对象", result.warnings[0])

    def test_data_dir_is_stripped(self) -> None:
        result = settings_mod.load_settings({"data_dir": "  D:/data/diary  "})
        self.assertEqual(result.data_dir, "D:/data/diary")

    # ---- proactive 组（第 4 步） ------------------------------------------------

    def test_proactive_int_clamps_into_range(self) -> None:
        result = settings_mod.load_settings(
            {"proactive": {"patrol_minutes": 0, "daily_limit_private": 99, "min_interval_minutes": -5}}
        )
        self.assertEqual(result.proactive["patrol_minutes"], 1)
        self.assertEqual(result.proactive["daily_limit_private"], 10)
        self.assertEqual(result.proactive["min_interval_minutes"], 0)
        self.assertEqual(len(result.warnings), 3, "三个越界项各记一条夹取告警")

    def test_proactive_wrong_type_falls_back_with_warning(self) -> None:
        result = settings_mod.load_settings({"proactive": {"patrol_minutes": "每刻钟", "signal_char": 5}})
        self.assertEqual(result.proactive["patrol_minutes"], 15)
        self.assertEqual(result.proactive["signal_char"], "。")
        self.assertEqual(len(result.warnings), 2)

    def test_proactive_signal_char_rejected_when_empty_or_too_long(self) -> None:
        result = settings_mod.load_settings({"proactive": {"signal_char": "  "}})
        self.assertEqual(result.proactive["signal_char"], "。")
        result = settings_mod.load_settings({"proactive": {"signal_char": "太长了不行"}})
        self.assertEqual(result.proactive["signal_char"], "。")

    def test_proactive_group_not_a_mapping_degrades_whole(self) -> None:
        result = settings_mod.load_settings({"proactive": [1, 2, 3]})
        self.assertEqual(dict(result.proactive), dict(settings_mod._PROACTIVE_DEFAULTS))
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("proactive 不是对象", result.warnings[0])


class SchemaConsistencyTest(unittest.TestCase):
    """``_conf_schema.json``（面板）与 ``settings.py``（代码）不许漂移。"""

    def setUp(self) -> None:
        path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        self.schema = json.loads(path.read_text(encoding="utf-8"))

    def test_top_level_keys_match_defaults(self) -> None:
        self.assertEqual(set(self.schema), set(settings_mod.default_config()))

    def test_subsystems_and_diary_subkeys_match(self) -> None:
        self.assertEqual(
            set(self.schema["subsystems"]["items"]), set(settings_mod.SUBSYSTEMS)
        )
        self.assertEqual(
            set(self.schema["diary"]["items"]),
            set(settings_mod.default_config()["diary"]),
        )
        self.assertEqual(
            set(self.schema["notebook"]["items"]),
            set(settings_mod.default_config()["notebook"]),
        )
        self.assertEqual(
            set(self.schema["state"]["items"]),
            set(settings_mod.default_config()["state"]),
        )
        self.assertEqual(
            set(self.schema["proactive"]["items"]),
            set(settings_mod.default_config()["proactive"]),
        )

    def test_every_diary_int_field_has_a_documented_range(self) -> None:
        for key in self.schema["diary"]["items"]:
            self.assertIn(f"diary.{key}", settings_mod.INT_LIMITS)
        for key in self.schema["notebook"]["items"]:
            self.assertIn(f"notebook.{key}", settings_mod.INT_LIMITS)
        for key, item in self.schema["proactive"]["items"].items():
            if item["type"] == "int":
                self.assertIn(f"proactive.{key}", settings_mod.INT_LIMITS)

    def test_schema_defaults_equal_code_defaults(self) -> None:
        defaults = settings_mod.default_config()
        for name in settings_mod.SUBSYSTEMS:
            self.assertEqual(self.schema["subsystems"]["items"][name]["default"], True)
        for key, value in defaults["diary"].items():
            self.assertEqual(self.schema["diary"]["items"][key]["default"], value)
        for key, value in defaults["notebook"].items():
            self.assertEqual(self.schema["notebook"]["items"][key]["default"], value)
        for key, value in defaults["state"].items():
            self.assertEqual(self.schema["state"]["items"][key]["default"], value)
        for key, value in defaults["proactive"].items():
            self.assertEqual(self.schema["proactive"]["items"][key]["default"], value)

    def test_remote_config_sample_loads_without_warnings(self) -> None:
        """面板能保存出来的形状（全部字段显式给值）不该触发任何降级告警。"""
        raw = {
            "enabled": True,
            "timezone": "Asia/Shanghai",
            "data_dir": "",
            "subsystems": dict.fromkeys(settings_mod.SUBSYSTEMS, True),
            "diary": dict(settings_mod.default_config()["diary"]),
            "notebook": dict(settings_mod.default_config()["notebook"]),
            "state": dict(settings_mod.default_config()["state"]),
            "proactive": dict(settings_mod.default_config()["proactive"]),
            "love_peers": [],
            "name_preference": {},
        }
        self.assertEqual(settings_mod.load_settings(raw).warnings, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
