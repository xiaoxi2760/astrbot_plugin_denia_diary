"""第 5.2 步「全部配置项搬进 WebUI 设置页」的离线验收。

覆盖任务书要求的那几条硬指标（§四「自测要求」）：

1. schema **递归展平**，五种类型（bool/string/int/list/dict）都出字段；
2. 类型不符 → **拒绝且一个字节都不写**；
3. 未知点分路径 → 拒绝；``data_dir`` → 拒绝（只读）；
4. **校验准绳是 ``load_settings`` 的 warnings**：越界/坏正则/坏时区/零宽正则全被挡下；
5. 落盘后 ``self.settings``（与各门面的引用）真的换了；
6. ``patrol_minutes`` 变了真的重建巡检 job，没变就不动；
7. 备份文件真的生成；备份/落盘失败**不写盘**且回落内存值；
8. ``POST`` 的坏 body → **400 不 500**。

纯离线：不连 AstrBot、不碰真实数据目录。``main.py`` 那一段借 ``test_main``
的 astrbot 桩加载**真**适配器代码（不复制第二份桩）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402
from test_main import _load_plugin  # noqa: E402

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core import webui_settings  # noqa: E402

PLUGIN_DIR = _HERE.parent
SCHEMA = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))


def schema_fields() -> list[dict]:
    return webui_settings.describe_schema(SCHEMA)[1]


# ---- 字段表 -------------------------------------------------------------------


class TestSchemaDescribe(unittest.TestCase):
    """字段表由 schema 驱动展开（前端不抄字段清单）。"""

    def setUp(self) -> None:
        self.groups, self.fields = webui_settings.describe_schema(SCHEMA)

    def test_top_level_groups_keep_schema_order(self) -> None:
        self.assertEqual(
            [node["key"] for node in self.groups], list(SCHEMA)
        )

    def test_five_leaf_types_are_all_covered(self) -> None:
        kinds = {field["type"] for field in self.fields}
        for kind in ("bool", "string", "int", "list", "dict"):
            self.assertIn(kind, kinds, f"{kind} 类型没被展开出来")

    def test_object_groups_carry_children_and_have_no_leaf(self) -> None:
        by_key = {node["key"]: node for node in self.groups}
        proactive = by_key["proactive"]
        self.assertEqual(proactive["type"], "object")
        self.assertEqual(proactive["path"], "proactive")
        self.assertTrue(proactive["description"] and proactive["hint"])
        self.assertNotIn("proactive", {field["path"] for field in self.fields})
        self.assertEqual(
            [child["key"] for child in proactive["children"]],
            list(SCHEMA["proactive"]["items"]),
        )

    def test_leaf_paths_are_dotted_and_have_defaults(self) -> None:
        by_path = {field["path"]: field for field in self.fields}
        for key in ("enabled", "timezone", "data_dir", "diary.max_chars", "proactive.patrol_minutes"):
            self.assertIn(key, by_path)
        self.assertTrue(by_path["enabled"]["default"] is True)
        self.assertEqual(by_path["diary.max_chars"]["default"], 1200)
        self.assertEqual(by_path["proactive.signal_char"]["default"], "。")

    def test_int_fields_get_min_max_from_settings_limits(self) -> None:
        by_path = {field["path"]: field for field in self.fields}
        self.assertEqual((by_path["diary.max_chars"]["min"], by_path["diary.max_chars"]["max"]), (200, 20000))
        self.assertEqual(
            (by_path["proactive.patrol_minutes"]["min"], by_path["proactive.patrol_minutes"]["max"]),
            (1, 59),
        )

    def test_multiline_flag_follows_default_shape(self) -> None:
        by_path = {field["path"]: field for field in self.fields}
        self.assertTrue(by_path["state.rhythm"]["multiline"])  # 默认值带换行 → textarea
        self.assertFalse(by_path["timezone"]["multiline"])

    def test_data_dir_is_read_only_with_a_reason(self) -> None:
        data_dir = {field["path"]: field for field in self.fields}["data_dir"]
        self.assertFalse(data_dir["editable"])
        self.assertTrue(data_dir["note"])
        self.assertNotIn("data_dir", webui_settings.editable_paths(self.fields))
        self.assertIn("diary.max_chars", webui_settings.editable_paths(self.fields))

    def test_every_schema_leaf_is_reachable(self) -> None:
        expected = {"enabled", "timezone", "data_dir", "love_peers", "name_preference"}
        for group, spec in SCHEMA.items():
            if spec.get("type") == "object":
                expected.update(f"{group}.{key}" for key in spec.get("items", {}))
        self.assertEqual({field["path"] for field in self.fields}, expected)

    def test_broken_schema_does_not_raise(self) -> None:
        groups, fields = webui_settings.describe_schema({"bad": "不是对象", "x": {"type": "list"}})
        self.assertEqual(len(groups), 1)
        self.assertEqual(fields[0]["default"], [])


# ---- 值与默认值 ---------------------------------------------------------------


class TestValuesAndDefaults(unittest.TestCase):
    def test_values_are_the_effective_snapshot_not_raw_config(self) -> None:
        resolved = settings_mod.load_settings({"diary": {"max_chars": 999999}, "timezone": "火星/某地"})
        values = webui_settings.values_from_settings(resolved, SCHEMA)
        self.assertEqual(values["diary"]["max_chars"], 20000)  # 已被夹紧 = 实际生效值
        self.assertEqual(values["timezone"], settings_mod.DEFAULT_TIMEZONE)  # 非法已回落

    def test_values_cover_nested_objects_and_top_level(self) -> None:
        values = webui_settings.values_from_settings(settings_mod.load_settings({}), SCHEMA)
        self.assertEqual(values["proactive"]["patrol_minutes"], 15)
        self.assertEqual(values["state"]["late_night"], "23:30-06:30")
        self.assertEqual(values["outbound"]["strip_meme_marks"], True)
        self.assertTrue(values["subsystems"]["diary"] is True)

    def test_tuples_become_lists_so_json_can_carry_them(self) -> None:
        values = webui_settings.values_from_settings(
            settings_mod.load_settings({"love_peers": ["u1", "u2"]}), SCHEMA
        )
        self.assertIsInstance(values["love_peers"], list)
        self.assertEqual(values["love_peers"], ["u1", "u2"])

    def test_defaults_come_from_schema(self) -> None:
        defaults = webui_settings.defaults_from_schema(SCHEMA)
        self.assertEqual(defaults["diary"]["max_chars"], 1200)
        self.assertEqual(defaults["love_peers"], [])
        self.assertEqual(defaults["name_preference"], {})
        self.assertEqual(defaults["enabled"], True)

    def test_fallback_schema_when_none_supplied(self) -> None:
        values = webui_settings.values_from_settings(settings_mod.load_settings({}))
        self.assertEqual(values["notebook"]["promise_limit"], 20)


# ---- 改动计划 -----------------------------------------------------------------


class TestPlanChanges(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.fields = schema_fields()

    def test_happy_path_merges_into_deep_copy(self) -> None:
        base = {"diary": {"max_chars": 1200}, "keep": "我不动"}
        updated, error, applied = webui_settings.plan_changes(
            base, {"diary.max_chars": 3000}, self.fields
        )
        self.assertEqual(error, "")
        self.assertEqual(applied, ["diary.max_chars"])
        self.assertEqual(updated["diary"]["max_chars"], 3000)
        self.assertEqual(base["diary"]["max_chars"], 1200)  # 底座没被就地改坏
        self.assertEqual(updated["keep"], "我不动")  # 未声明的键原样保留

    def test_unknown_path_is_rejected(self) -> None:
        updated, error, applied = webui_settings.plan_changes({}, {"diary.nope": 1}, self.fields)
        self.assertIsNone(updated)
        self.assertIn("未知", error)
        self.assertEqual(applied, [])

    def test_unknown_top_level_path_is_rejected(self) -> None:
        updated, error, _ = webui_settings.plan_changes({}, {"hack": {"a": 1}}, self.fields)
        self.assertIsNone(updated)
        self.assertIn("未知", error)

    def test_group_itself_is_not_a_settable_path(self) -> None:
        updated, error, _ = webui_settings.plan_changes({}, {"diary": {"max_chars": 1}}, self.fields)
        self.assertIsNone(updated)
        self.assertIn("未知", error)

    def test_data_dir_is_rejected_with_reason(self) -> None:
        updated, error, _ = webui_settings.plan_changes({}, {"data_dir": "/tmp/x"}, self.fields)
        self.assertIsNone(updated)
        self.assertIn("只能看不能改", error)

    def test_type_mismatch_rejected_per_type(self) -> None:
        cases = {
            "enabled": "true",               # 字符串给 bool
            "timezone": 5,                   # 数字给 string
            "diary.max_chars": "3000",       # 字符串给 int
            "proactive.patrol_minutes": True,  # bool 给 int（isinstance 会放过，type() 不放过）
            "love_peers": "u1",              # 字符串给 list
            "love_peers": [1, 2],            # 数字列表给 list
            "name_preference": ["u1"],       # 列表给 dict
        }
        for path, value in cases.items():
            updated, error, _ = webui_settings.plan_changes({}, {path: value}, self.fields)
            self.assertIsNone(updated, f"{path}={value!r} 竟然被放行了")
            self.assertIn("值类型不对", error, f"{path} 的报错口径不对")

    def test_empty_or_non_dict_changes_rejected(self) -> None:
        for bad in ({}, None, "x", [1]):
            updated, error, _ = webui_settings.plan_changes({}, bad, self.fields)
            self.assertIsNone(updated)
            self.assertIn("没有需要保存", error)

    def test_one_bad_key_rejects_the_whole_batch(self) -> None:
        """一半好一半坏也整单拒绝——不写一半。"""
        updated, error, applied = webui_settings.plan_changes(
            {}, {"diary.max_chars": 3000, "data_dir": "/tmp"}, self.fields
        )
        self.assertIsNone(updated)
        self.assertIn("只能看不能改", error)
        self.assertEqual(applied, [])

    def test_intermediate_non_object_is_rejected_not_crashed(self) -> None:
        updated, error, _ = webui_settings.plan_changes(
            {"diary": "我不是对象"}, {"diary.max_chars": 3000}, self.fields
        )
        self.assertIsNone(updated)
        self.assertIn("配置结构异常", error)

    def test_missing_intermediate_object_is_created(self) -> None:
        updated, error, _ = webui_settings.plan_changes({}, {"notebook.fact_limit": 9}, self.fields)
        self.assertEqual(error, "")
        self.assertEqual(updated["notebook"]["fact_limit"], 9)


# ---- 校验准绳：load_settings 的 warnings ---------------------------------------


class TestVerifySettings(unittest.TestCase):
    def test_clean_change_passes(self) -> None:
        resolved, fresh = webui_settings.verify_settings({}, {"proactive": {"patrol_minutes": 30}})
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.proactive["patrol_minutes"], 30)
        self.assertEqual(fresh, [])

    def test_out_of_range_int_is_blocked(self) -> None:
        resolved, fresh = webui_settings.verify_settings({}, {"proactive": {"patrol_minutes": 999}})
        self.assertIsNone(resolved)
        self.assertTrue(any("patrol_minutes" in item for item in fresh))

    def test_broken_regex_is_blocked(self) -> None:
        resolved, fresh = webui_settings.verify_settings(
            {}, {"outbound": {"marker_pattern": "("}}
        )
        self.assertIsNone(resolved)
        self.assertTrue(any("marker_pattern" in item for item in fresh))

    def test_zero_width_regex_is_blocked(self) -> None:
        resolved, fresh = webui_settings.verify_settings(
            {}, {"outbound": {"marker_pattern": "x*"}}
        )
        self.assertIsNone(resolved)
        self.assertTrue(fresh)

    def test_broken_timezone_is_blocked(self) -> None:
        resolved, fresh = webui_settings.verify_settings({}, {"timezone": "火星/某地"})
        self.assertIsNone(resolved)
        self.assertTrue(any("时区" in item for item in fresh))

    def test_bad_sub_group_shape_is_blocked(self) -> None:
        resolved, _ = webui_settings.verify_settings({}, {"subsystems": {"diary": "是"}})
        self.assertIsNone(resolved)

    def test_pre_existing_warning_does_not_block_unrelated_change(self) -> None:
        """本来就坏的配置不该让面板**每个改动**都改不动。"""
        base = {"outbound": {"marker_pattern": "("}}  # 开机就报 warning
        resolved, fresh = webui_settings.verify_settings(base, {"proactive": {"patrol_minutes": 30}})
        self.assertIsNotNone(resolved, "既有 warning 不该被算成新问题")
        self.assertEqual(fresh, [])

    def test_fixing_a_pre_existing_warning_is_allowed(self) -> None:
        base = {"outbound": {"marker_pattern": "("}}
        resolved, fresh = webui_settings.verify_settings(base, {"outbound": {"marker_pattern": "&&x&&"}})
        self.assertIsNotNone(resolved)
        self.assertEqual(fresh, [])

    def test_warnings_are_carried_into_the_result(self) -> None:
        """既有降级项要跟着结果一起走，让面板看得见"她在按默认值跑"。"""
        base = {"timezone": "火星/某地"}  # 开机就回落，仍记着 warning
        updated, _, _ = webui_settings.plan_changes(base, {"diary.max_chars": 3000}, schema_fields())
        resolved, fresh = webui_settings.verify_settings(base, updated)
        self.assertTrue(resolved is not None)
        self.assertTrue(resolved.warnings)
        self.assertEqual(fresh, [])


# ---- 备份 ---------------------------------------------------------------------


class TestBackup(TmpDirCase):
    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_backup_is_copied_into_data_dir(self) -> None:
        source = self.write("config.json", '{"a": 1}')
        target = webui_settings.backup_config_file(
            source, self.root / "data", datetime(2026, 10, 6, 1, 2, 3)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "config_backup_20261006-010203.json")
        self.assertEqual(target.parent, self.root / "data")
        self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')
        self.assertEqual(source.read_text(encoding="utf-8"), '{"a": 1}')  # 原件没动

    def test_missing_or_unknown_source_skips_backup(self) -> None:
        self.assertIsNone(webui_settings.backup_config_file(None, self.root))
        self.assertIsNone(webui_settings.backup_config_file("", self.root))
        self.assertIsNone(webui_settings.backup_config_file(self.root / "没有.json", self.root))

    def test_backup_name_shape(self) -> None:
        name = webui_settings.backup_name(datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(name, "config_backup_20260102-030405.json")

    def test_data_dir_is_created_on_demand(self) -> None:
        source = self.write("c.json", "{}")
        target = webui_settings.backup_config_file(source, self.root / "deep" / "data")
        self.assertTrue(target.parent.is_dir())


# ---- handler 薄壳 -------------------------------------------------------------


class DictConfig(dict):
    """带 ``schema`` 属性的配置对象（``AstrBotConfig`` 是 dict 子类，也有 schema）。"""

    def __init__(self, payload=None, schema=SCHEMA):
        super().__init__(payload or {})
        self.schema = schema
        self.config_path = None


async def _async_value(value):
    return value


class TestSettingsHandlers(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers, routes

        self.handlers = handlers
        self.routes = routes
        self.layout = storage.Layout(self.root).ensure()
        self.raw = {"timezone": "Asia/Shanghai", "proactive": {"patrol_minutes": 15}}
        self.applied: list[dict] = []

        async def apply_settings(updated, *, applied=None):
            self.applied.append(updated)
            return {"ok": True, "applied": list(applied or []), "warnings": [], "reloaded": False, "backup": "b.json"}

        self.config = DictConfig(self.raw)
        self.deps = SimpleNamespace(
            config=self.config,
            settings=settings_mod.load_settings(self.raw),
            layout=self.layout,
            apply_settings=apply_settings,
        )

    def call(self, name: str, body=None):
        fake = SimpleNamespace(
            query={},
            username="admin",
            json=lambda default=None: _async_value(body if body is not None else default),
        )
        original = self.handlers.request
        self.handlers.request = fake
        try:
            handler = self.handlers.build_handlers(self.deps)[name]
            return asyncio.run(self.handlers.logged_handler(name, handler)())
        finally:
            self.handlers.request = original

    def data_of(self, response):
        self.assertEqual(response["_stub"], "json_response")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def test_route_table_has_both_settings_verbs(self) -> None:
        settings_routes = [spec for spec in self.routes.ROUTES if spec[0] == "settings"]
        self.assertEqual([spec[2] for spec in settings_routes], [("GET",), ("POST",)])
        self.assertEqual([spec[1] for spec in settings_routes], ["settings_get", "settings_post"])

    def test_get_returns_the_whole_field_snapshot(self) -> None:
        data = self.data_of(self.call("settings_get"))
        for key in ("ok", "groups", "fields", "values", "defaults", "warnings", "editable_paths"):
            self.assertIn(key, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["fields"])
        self.assertIn("diary.max_chars", data["editable_paths"])
        self.assertNotIn("data_dir", data["editable_paths"])
        self.assertEqual(data["values"]["diary"]["max_chars"], 1200)
        self.assertEqual(data["defaults"]["diary"]["max_chars"], 1200)
        self.assertIsInstance(data["warnings"], list)

    def test_get_falls_back_to_the_schema_file_when_config_has_none(self) -> None:
        del self.config.schema
        data = self.data_of(self.call("settings_get"))
        self.assertTrue(data["fields"], "配置对象没有 schema 时应回落到 _conf_schema.json")

    def test_post_happy_path_applies(self) -> None:
        data = self.data_of(
            self.call("settings_post", body={"changes": {"proactive.patrol_minutes": 30}})
        )
        self.assertTrue(data["ok"])
        self.assertEqual(data["applied"], ["proactive.patrol_minutes"])
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.applied[0]["proactive"]["patrol_minutes"], 30)
        self.assertEqual(self.applied[0]["timezone"], "Asia/Shanghai")  # 其余项原样带上

    def test_post_bad_body_is_400(self) -> None:
        for body in ({}, {"changes": {}}, {"changes": "x"}, None, [1, 2]):
            response = self.call("settings_post", body=body)
            self.assertEqual(response["_stub"], "error_response", f"body={body!r}")
            self.assertEqual(response["status"], 400)
        self.assertEqual(self.applied, [], "请求形状不对时绝不能调适配器")

    def test_post_unknown_key_is_400_and_writes_nothing(self) -> None:
        response = self.call("settings_post", body={"changes": {"diary.nope": 1}})
        self.assertEqual(response["status"], 400)
        self.assertIn("未知", response["message"])
        self.assertEqual(self.applied, [])

    def test_post_type_mismatch_is_400_and_writes_nothing(self) -> None:
        response = self.call("settings_post", body={"changes": {"diary.max_chars": "很多"}})
        self.assertEqual(response["status"], 400)
        self.assertIn("值类型不对", response["message"])
        self.assertEqual(self.applied, [])

    def test_post_data_dir_is_400_and_writes_nothing(self) -> None:
        response = self.call("settings_post", body={"changes": {"data_dir": "/tmp/x"}})
        self.assertEqual(response["status"], 400)
        self.assertIn("只能看不能改", response["message"])
        self.assertEqual(self.applied, [])

    def test_post_value_rejected_by_load_settings_writes_nothing(self) -> None:
        response = self.call("settings_post", body={"changes": {"proactive.patrol_minutes": 999}})
        self.assertEqual(response["status"], 400)
        self.assertIn("没通过校验", response["message"])
        self.assertTrue(response["extra"].get("problems"))
        self.assertEqual(self.applied, [], "被 warnings 挡住时一个字节都不写")

    def test_post_broken_regex_is_400(self) -> None:
        response = self.call("settings_post", body={"changes": {"outbound.marker_pattern": "("}})
        self.assertEqual(response["status"], 400)
        self.assertEqual(self.applied, [])

    def test_post_reports_enabled_notice(self) -> None:
        data = self.data_of(self.call("settings_post", body={"changes": {"enabled": False}}))
        self.assertTrue(any("不可用" in item for item in data.get("notices") or []))

    def test_adapter_failure_is_200_with_ok_false(self) -> None:
        async def boom(updated, *, applied=None):
            return {"ok": False, "error": "备份旧配置失败，未做改动：磁盘满了"}

        self.deps.apply_settings = boom
        data = self.data_of(
            self.call("settings_post", body={"changes": {"diary.max_chars": 3000}})
        )
        self.assertFalse(data["ok"])
        self.assertIn("备份", data["error"])

    def test_handler_layer_never_raises_500_for_garbage(self) -> None:
        class Exploding:
            schema = SCHEMA

            def keys(self):
                raise RuntimeError("配置对象炸了")

            def items(self):
                raise RuntimeError("配置对象炸了")

            @property
            def enabled(self):  # 取生效值时炸 → 必须结构化，不能冒出 500 之外的东西
                raise RuntimeError("配置对象炸了")

        self.deps.config = Exploding()
        self.deps.settings = Exploding()
        response = self.call("settings_get")
        self.assertEqual(response["_stub"], "error_response")
        self.assertEqual(response["status"], 500)
        self.assertIn("日志", response["message"])


# ---- 真适配器：main.py 的 apply_settings ---------------------------------------


class FakeConfig(dict):
    """``AstrBotConfig`` 的最小替身：dict 子类 + ``schema`` + ``save_config``。

    刻意**不**在 ``save_config`` 里回写内存——模拟"只写盘不回写"的那种宿主版本，
    这样"落盘后 ``self.config`` 与 ``self.settings`` 都换了"才是被真正验过的，
    而不是被替身放水放过去的。
    """

    def __init__(self, payload=None, path=None):
        super().__init__(payload or {})
        self.schema = SCHEMA
        self.config_path = path
        self.saves: list[dict] = []
        self.fail_save = False

    def save_config(self, data) -> None:
        if self.fail_save:
            raise OSError("磁盘满了")
        self.saves.append(dict(data))
        if self.config_path:
            Path(self.config_path).write_text(
                json.dumps(dict(data), ensure_ascii=False), encoding="utf-8"
            )


class FakeCron:
    def __init__(self) -> None:
        # 装一个"上次留下的僵尸 job"：重建必须是 delete→add
        self.jobs: list[object] = [SimpleNamespace(name="astrbot_plugin_denia_diary#proactive-patrol", job_id="stale-1")]
        self.added: list[dict] = []
        self.deleted: list[str] = []

    async def list_jobs(self):
        return list(self.jobs)

    async def delete_job(self, job_id) -> None:
        self.deleted.append(job_id)
        self.jobs = [job for job in self.jobs if getattr(job, "job_id", None) != job_id]

    async def add_basic_job(self, **kwargs) -> None:
        self.added.append(kwargs)
        self.jobs.append(SimpleNamespace(name=kwargs.get("name"), job_id=f"job{len(self.added)}"))


class TestApplySettingsAdapter(TmpDirCase):
    """**真** ``main.py`` 的适配器方法（借 test_main 的 astrbot 桩加载）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.module, _ = _load_plugin(Path.cwd())

    def setUp(self) -> None:
        super().setUp()
        self.cron = FakeCron()
        self.config_file = self.root / "config.json"
        self.config = FakeConfig({"timezone": "Asia/Shanghai"}, path=self.config_file)
        # 数据目录指到临时区：apply_settings 的备份要落在这里（别写进工作区）
        self.module.StarTools.data_dir = str(self.root / "data")
        self.plugin = self.module.DeniaDiary(
            context=SimpleNamespace(cron_manager=self.cron), config=self.config
        )
        self.diary, self.notebook, self.state = self.plugin.diary, self.plugin.notebook, self.plugin.state
        self.affinity, self.proactive = self.plugin.affinity, self.plugin.proactive

    def run_async(self, coro):
        return asyncio.run(coro)

    def apply(self, updated, applied):
        return self.run_async(self.plugin.apply_settings(updated, applied=applied))

    def test_settings_object_is_rebuilt_in_place(self) -> None:
        before = self.plugin.settings
        result = self.apply({"timezone": "UTC"}, ["timezone"])
        self.assertTrue(result["ok"])
        self.assertIsNot(self.plugin.settings, before, "self.settings 必须换成一个新快照")
        self.assertEqual(self.plugin.settings.timezone, "UTC")
        self.assertTrue(self.plugin._now().tzinfo is not None)

    def test_facades_see_the_new_settings(self) -> None:
        for facade in (self.diary, self.notebook, self.state, self.affinity, self.proactive):
            self.assertIs(facade.settings, self.plugin.settings, "门面还攥着旧快照 = 改了不生效")
        self.apply({"proactive": {"daily_limit_private": 5}}, ["proactive.daily_limit_private"])
        self.assertEqual(self.plugin.proactive.settings.proactive["daily_limit_private"], 5)
        self.assertEqual(self.diary.settings.proactive["daily_limit_private"], 5)

    def test_config_is_written_through_save_config(self) -> None:
        result = self.apply({"diary": {"max_chars": 3000}}, ["diary.max_chars"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.config.saves), 1)
        self.assertEqual(self.config.saves[0]["diary"]["max_chars"], 3000)
        self.assertEqual(dict(self.config)["diary"]["max_chars"], 3000)
        self.assertTrue(self.config_file.is_file())

    def test_backup_file_is_really_generated(self) -> None:
        self.config_file.write_text('{"timezone": "Asia/Shanghai"}', encoding="utf-8")
        result = self.apply({"timezone": "UTC"}, ["timezone"])
        self.assertTrue(result["backup"])
        backup = self.plugin.layout.base_dir / result["backup"]
        self.assertTrue(backup.is_file(), "备份文件没生成")
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["timezone"], "Asia/Shanghai")

    def test_patrol_minutes_change_rebuilds_the_job(self) -> None:
        self.apply({"proactive": {"patrol_minutes": 15}}, [])
        self.cron.added.clear()
        result = self.apply({"proactive": {"patrol_minutes": 25}}, ["proactive.patrol_minutes"])
        self.assertTrue(result["reloaded"])
        self.assertEqual(len(self.cron.added), 1)
        self.assertEqual(self.cron.added[0]["cron_expression"], "*/25 * * * *")
        self.assertTrue(self.cron.deleted, "旧 job 必须先删再建")

    def test_unrelated_change_does_not_touch_the_job(self) -> None:
        self.apply({"proactive": {"patrol_minutes": 15}}, [])
        self.cron.added.clear()
        result = self.apply({"diary": {"max_chars": 3000}}, ["diary.max_chars"])
        self.assertFalse(result["reloaded"])
        self.assertEqual(self.cron.added, [])

    def test_timezone_change_does_not_rebuild_job(self) -> None:
        result = self.apply({"timezone": "UTC"}, ["timezone"])
        self.assertFalse(result["reloaded"])

    def test_save_failure_reports_error_and_rolls_memory_back(self) -> None:
        before = dict(self.config)
        self.config.fail_save = True
        result = self.apply({"timezone": "UTC"}, ["timezone"])
        self.assertFalse(result["ok"])
        self.assertIn("写配置失败", result["error"])
        self.assertEqual(dict(self.config), before, "内存里必须回落到旧值")
        self.assertEqual(self.plugin.settings.timezone, before.get("timezone", "Asia/Shanghai"))

    def test_backup_failure_prevents_any_write(self) -> None:
        """备份是安全网：备份不下来就整单放弃，配置一个字节都不改。"""
        self.config_file.write_text("{}", encoding="utf-8")
        # 注意补丁打在**插件实际用的那个模块对象**上：``main.py`` 走
        # ``plugin_under_test.core.*``，和本测试 import 的 ``core.*`` 是两份。
        target = self.module.webui_settings
        original = target.backup_config_file

        def boom(*args, **kwargs):
            raise OSError("数据目录不可写")

        target.backup_config_file = boom
        self.addCleanup(setattr, target, "backup_config_file", original)

        result = self.apply({"timezone": "UTC"}, ["timezone"])
        self.assertFalse(result["ok"])
        self.assertIn("备份", result["error"])
        self.assertEqual(self.config.saves, [], "备份失败时绝不能写配置")
        self.assertEqual(self.plugin.settings.timezone, "Asia/Shanghai")

    def test_result_carries_applied_warnings_and_backup(self) -> None:
        result = self.apply({"proactive": {"patrol_minutes": 20}}, ["proactive.patrol_minutes"])
        for key in ("ok", "applied", "warnings", "reloaded", "backup"):
            self.assertIn(key, result)
        self.assertIsInstance(result["warnings"], list)
        self.assertEqual(result["applied"], ["proactive.patrol_minutes"])


if __name__ == "__main__":
    unittest.main()
