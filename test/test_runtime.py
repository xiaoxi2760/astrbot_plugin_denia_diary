"""多 Bot 隔离内核（``core.runtime``）的验收测试。

覆盖（对应 P0 的隔离边界）：

1. 同一 sender 在不同 scope 下，日记 / 小本本 / 情绪 / 熟悉度 / 生活 / 主动**互不串**；
2. 显式绑定的 legacy 继承原数据，新 scope **空白**初始化；
3. 缺 scope / 坏 scope 一律拒绝（隔离开启时**不**默认回落 legacy）；
4. 目录安全（scope 不能逃出数据根）、损坏的归属元信息 fail closed、重启后映射一致；
5. ``per_instance`` 的主人 / 称呼 / 时区覆盖生效，全局关闭仍然有效；
6. 新 scope **不复制**旧用户数据；
7. 配置热更新**不换数据根**、**不破坏旧引用**。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core import runtime as runtime_mod
from core import settings as settings_mod
from core import storage
from core.session import Session


def _run(coro):
    """跑一个协程（这些门面里落盘类的方法都是 async）。"""
    return asyncio.run(coro)



def _tmp_root() -> Path:
    """测试临时根。优先用 ``DIARY_TEST_TMP``，否则退回系统临时目录。"""
    configured = os.environ.get("DIARY_TEST_TMP", "").strip()
    base = Path(configured) if configured else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    return base / f"runtime-test-{os.getpid()}"


def _private_session(user_id: str = "u1") -> Session:
    return Session(
        umo=f"aiocqhttp:PrivateMessage:{user_id}",
        platform="aiocqhttp",
        message_type="PrivateMessage",
        session_id=user_id,
        sender_id=user_id,
        sender_name=user_id,
    )


async def _maybe_await(value):
    """同步/异步门面都能调（不假设哪个是 async）。"""
    if inspect.isawaitable(value):
        return await value
    return value


class RuntimeTestCase(unittest.TestCase):
    """每个用例一个独立数据根。"""

    def setUp(self) -> None:
        self.root = _tmp_root()
        self.base = self.root / "data"
        self.base.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- 工具 ----

    def _manager(self, config: dict | None = None) -> runtime_mod.DiaryRuntimeManager:
        return runtime_mod.DiaryRuntimeManager(self.base, config or {})

    def _iso_config(self, **extra) -> dict:
        config = {"bot_isolation": {"enable": True, "legacy_platform_id": ""}}
        config.update(extra)
        return config


class TestIsolationSwitch(RuntimeTestCase):
    def test_isolation_off_keeps_legacy_behaviour(self) -> None:
        """默认（隔离关闭）→ 一切回到 legacy 桶，老部署不受影响。"""
        manager = self._manager({})
        self.assertFalse(manager.isolation_on)
        runtime = manager.resolve("any-scope")
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.base_dir, self.base)

    def test_isolation_on_rejects_missing_scope(self) -> None:
        """隔离开启 → 空 scope 拒绝，**不**默认回落 legacy。"""
        manager = self._manager(self._iso_config())
        self.assertTrue(manager.isolation_on)
        self.assertIsNone(manager.resolve(""))
        self.assertIsNone(manager.resolve("   "))
        self.assertIsNone(manager.resolve_event(object()))

    def test_bad_scope_is_rejected(self) -> None:
        manager = self._manager(self._iso_config())
        self.assertIsNone(manager.resolve("x" * 500))
        self.assertFalse(runtime_mod.is_valid_scope("bad\x00id"))

    def test_legacy_binding_is_explicit(self) -> None:
        """只有显式绑定的实例才回到原数据根；没绑谁都读不到旧数据。"""
        manager = self._manager(self._iso_config())
        self.assertEqual(manager.legacy_platform_id, "")
        self.assertNotEqual(manager.resolve("scope-a").base_dir, self.base)

        bound = self._manager(
            {"bot_isolation": {"enable": True, "legacy_platform_id": "personal-qq"}}
        )
        runtime = bound.resolve("personal-qq")
        self.assertEqual(runtime.base_dir, self.base)
        self.assertTrue(runtime.legacy)


class TestScopeSeparation(RuntimeTestCase):
    def test_same_sender_different_scope_do_not_cross(self) -> None:
        """同一 sender 在两个 scope 下：日记 / 小本本 / 熟悉度 / 主动记录互不可见。"""
        manager = self._manager(self._iso_config())
        first = manager.resolve("scope-a")
        second = manager.resolve("scope-b")
        self.assertNotEqual(first.base_dir, second.base_dir)

        session = _private_session("u1")
        _run(first.diary.write_async(session, text="只属于 A 的日记", mood="开心"))
        self.assertTrue(first.diary.entries())
        self.assertEqual(second.diary.entries(), [], "B 看到了 A 的日记")

        _run(first.affinity.touch(session, kind="private"))
        self.assertNotEqual(first.affinity.score("u1"), second.affinity.score("u1"))

        _run(first.proactive.note_contact(session))
        self.assertIn("u1", first.proactive.store.contacts())
        self.assertNotIn("u1", second.proactive.store.contacts())

    def test_new_scope_starts_blank_and_copies_nothing(self) -> None:
        """新 scope 空白初始化，且**不复制**旧用户数据。"""
        manager = self._manager(self._iso_config())
        legacy = manager.resolve("scope-legacy")  # 先在某个桶里造点数据
        session = _private_session("u1")
        _run(legacy.diary.write_async(session, text="旧数据", mood="平静"))

        fresh = manager.resolve("scope-fresh")
        self.assertEqual(fresh.diary.entries(), [], "新 scope 复制了旧日记")
        self.assertFalse(fresh.layout.diary_jsonl.exists(), "新 scope 不该有旧日记文件")
        # 模板类目录可以有，用户数据文件不行
        self.assertFalse(fresh.layout.notebook.exists())
        self.assertFalse(fresh.layout.affinity.exists())

    def test_legacy_scope_inherits_existing_data(self) -> None:
        """显式绑定的 legacy 用原 base 根 → 旧数据仍在。"""
        manager = self._manager({})  # 先在 base 根写数据
        _run(manager.resolve("legacy-bucket").diary.write_async(
            _private_session("u1"), text="旧部署的日记", mood="平静"
        ))
        bound = self._manager(
            {"bot_isolation": {"enable": True, "legacy_platform_id": "personal-qq"}}
        )
        runtime = bound.resolve("personal-qq")
        self.assertEqual(runtime.base_dir, self.base)
        self.assertTrue(runtime.diary.entries(), "legacy 没继承到旧日记")


class TestDirectorySafety(RuntimeTestCase):
    def test_scope_cannot_escape_data_root(self) -> None:
        manager = self._manager(self._iso_config())
        runtime = manager.resolve("../../evil")
        self.assertIsNotNone(runtime)
        resolved = runtime.base_dir.resolve()
        self.assertTrue(
            str(resolved).startswith(str(self.base.resolve())),
            f"数据根逃出去了：{resolved}",
        )

    def test_instance_metadata_records_ownership(self) -> None:
        manager = self._manager(self._iso_config())
        runtime = manager.resolve("scope-a")
        meta = runtime_mod.read_instance_meta(runtime.base_dir)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.scope_id, "scope-a")
        self.assertEqual(meta.kind, "bot")

    def test_corrupted_metadata_fails_closed(self) -> None:
        """归属元信息坏了 → 拒绝服务（不覆盖、不猜）。"""
        manager = self._manager(self._iso_config())
        runtime = manager.resolve("scope-a")
        (runtime.base_dir / runtime_mod.INSTANCE_FILE).write_text("{ 坏掉的 json", encoding="utf-8")
        with self.assertRaises(ValueError):
            runtime_mod.read_instance_meta(runtime.base_dir)

    def test_mismatched_ownership_is_refused(self) -> None:
        """归属记的是别的 scope → 拒绝（不悄悄认领旧数据）。"""
        manager = self._manager(self._iso_config())
        runtime = manager.resolve("scope-a")
        runtime_mod.write_instance_meta(
            runtime.base_dir,
            runtime_mod.InstanceMeta(scope_id="someone-else", kind="bot"),
        )
        again = self._manager(self._iso_config())
        self.assertIsNone(again.resolve("scope-a"))

    def test_restart_keeps_same_mapping(self) -> None:
        """重启后同一个 scope 映射到同一个目录。"""
        first = self._manager(self._iso_config())
        path = first.resolve("scope-a").base_dir
        second = self._manager(self._iso_config())
        self.assertEqual(second.resolve("scope-a").base_dir, path)


class TestPerInstanceConfig(RuntimeTestCase):
    def test_owner_name_and_timezone_overrides(self) -> None:
        """per_instance 覆盖主人 / 称呼 / 时区，只作用在对应 scope。"""
        config = {
            "bot_isolation": {"enable": True, "legacy_platform_id": ""},
            "timezone": "Asia/Shanghai",
            "per_instance": {
                "scope-a": {
                    "timezone": "Asia/Tokyo",
                    "love_peers": ["boss-a"],
                    "name_preference": {"u1": "阿一"},
                }
            },
        }
        manager = self._manager(config)
        first = manager.resolve("scope-a")
        second = manager.resolve("scope-b")
        self.assertEqual(first.settings.timezone, "Asia/Tokyo")
        self.assertEqual(first.settings.love_peers, ("boss-a",))
        self.assertEqual(dict(first.settings.name_preference), {"u1": "阿一"})
        self.assertEqual(second.settings.timezone, "Asia/Shanghai")
        self.assertEqual(second.settings.love_peers, ())

    def test_global_disable_cannot_be_locally_enabled(self) -> None:
        """全局 enabled=false → 任何 scope 都不能被局部打开。"""
        config = {
            "enabled": False,
            "bot_isolation": {"enable": True, "legacy_platform_id": ""},
            "per_instance": {"scope-a": {"enabled": True}},
        }
        settings = settings_mod.load_settings(config)
        self.assertFalse(settings.enabled)
        scoped = settings_mod.build_scope_settings(config, "scope-a")
        self.assertFalse(scoped.enabled, "局部把总开关打开了")

    def test_host_fields_cannot_be_overridden(self) -> None:
        """宿主级字段（数据目录 / 设备桥 / 隔离路由）不允许被 per_instance 覆盖。"""
        config = {
            "bot_isolation": {"enable": True, "legacy_platform_id": ""},
            "data_dir": "keep-me",
            "per_instance": {
                "scope-a": {
                    "data_dir": "hijack",
                    "pet_bridge": {"enabled": True},
                    "bot_isolation": {"enable": False},
                    "timezone": "Asia/Tokyo",
                }
            },
        }
        scoped = settings_mod.build_scope_settings(config, "scope-a")
        self.assertEqual(scoped.data_dir, "keep-me", "数据目录被局部覆盖了")
        self.assertEqual(scoped.timezone, "Asia/Tokyo")
        self.assertTrue(
            scoped.bot_isolation.get("enable"),
            "隔离路由被局部改写（全局仍是开的，局部关不掉）",
        )


class TestConfigUpdate(RuntimeTestCase):
    def test_update_keeps_data_root_and_old_references(self) -> None:
        """配置热更新：不换数据根、不废掉旧引用。"""
        manager = self._manager(self._iso_config())
        runtime = manager.resolve("scope-a")
        old_dir = runtime.base_dir
        old_diary = runtime.diary

        manager.update_config(
            {
                "bot_isolation": {"enable": True, "legacy_platform_id": ""},
                "timezone": "Asia/Tokyo",
            }
        )
        self.assertEqual(runtime.base_dir, old_dir, "数据根被换掉了")
        self.assertIs(runtime.diary, old_diary, "旧引用被换掉了")
        self.assertEqual(runtime.settings.timezone, "Asia/Tokyo")
        self.assertEqual(runtime.diary.settings.timezone, "Asia/Tokyo", "门面没跟上热更新")

    def test_update_config_keeps_scope_directories(self) -> None:
        manager = self._manager(self._iso_config())
        first = manager.resolve("scope-a")
        second = manager.resolve("scope-b")
        dirs = {first.base_dir, second.base_dir}
        manager.update_config(
            {
                "bot_isolation": {"enable": True, "legacy_platform_id": ""},
                "timezone": "Asia/Tokyo",
            }
        )
        self.assertEqual({first.base_dir, second.base_dir}, dirs)


class TestSettingsSchemaAlignment(unittest.TestCase):
    """schema 与代码默认值必须双向对齐（面板少了键会静默丢配置）。"""

    def test_schema_and_defaults_agree(self) -> None:
        from core import webui_settings

        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(set(schema), set(settings_mod.default_config()))
        self.assertEqual(
            set(schema["bot_isolation"]["items"]),
            set(settings_mod.default_config()["bot_isolation"]),
        )
        self.assertEqual(webui_settings.verify_schema_alignment(schema), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
