"""适配器测试（第 1 步）：**stub 掉 ``astrbot.api`` 后加载真实的 ``main.py``**。

做法来自 ``dev-examples/plugin-dev-notes.md`` 第三节：把最小可用的 ``astrbot.api`` 塞进
``sys.modules``，再按 AstrBot 的包路径（``<pkg>.main``）加载插件，这样 ``main.py`` 里的
相对导入（``from .core import ...``）与真实运行时一致。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import NOW, TmpDirCase  # noqa: E402

SUPPORTED_TYPES = {"string", "number", "boolean", "object", "array"}
MODULE_NAME = "plugin_under_test.main"


def _install_stubs(default_data_dir: Path) -> dict:
    """最小 astrbot.api 替身：只实现 main.py 用到的东西，并记录注册结果。"""
    registry: dict = {"tools": {}, "hooks": [], "plugin": None, "cls": None}

    class _Filter:
        def _record(self, kind: str, name: str | None, kwargs: dict):
            def decorator(func):
                if kind == "tool":
                    registry["tools"][name or func.__name__] = func
                else:
                    registry["hooks"].append((kind, name, func, kwargs))
                return func

            return decorator

        def llm_tool(self, name: str | None = None, **kwargs):
            return self._record("tool", name, kwargs)

        def on_llm_request(self, **kwargs):
            return self._record("on_llm_request", None, kwargs)

        def on_using_llm_tool(self, **kwargs):
            return self._record("on_using_llm_tool", None, kwargs)

        def command(self, name=None, **kwargs):
            return self._record("command", name, kwargs)

        def regex(self, *args, **kwargs):
            return self._record("regex", None, kwargs)

        def event_message_type(self, *args, **kwargs):
            return self._record("event_message_type", None, kwargs)

    class AstrMessageEvent:
        """真实事件由 AstrBot 构造；测试里用 ``_FakeEvent`` 代替。"""

    class ProviderRequest:
        def __init__(self) -> None:
            self.system_prompt = ""
            self.prompt = ""
            self.contexts: list = []

    class Context:
        pass

    class Star:
        def __init__(self, context=None) -> None:
            self.context = context

    class StarTools:
        data_dir = default_data_dir
        sent: list = []

        @classmethod
        def get_data_dir(cls, plugin_name=None) -> str:
            return str(cls.data_dir)

        @classmethod
        def initialize(cls, context) -> None:
            return None

        @classmethod
        async def send_message(cls, session, message_chain) -> bool:
            """暗号直发的替身：返回值即"是否送达"（真实现是平台是否找到）。"""
            cls.sent.append((session, message_chain))
            return getattr(cls, "send_result", True)

    class MessageChain:
        def __init__(self, chain=None) -> None:
            self.chain = chain or []

    class Plain:
        def __init__(self, text=None) -> None:
            self.text = text

    def register(name, author, desc, version):
        registry["plugin"] = (name, author, desc, version)

        def decorator(cls):
            registry["cls"] = cls
            return cls

        return decorator

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    provider = types.ModuleType("astrbot.api.provider")
    star_module = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.api.message_components")

    event.AstrMessageEvent = AstrMessageEvent
    event.MessageChain = MessageChain
    event.filter = _Filter()
    provider.ProviderRequest = ProviderRequest
    provider.LLMResponse = type("LLMResponse", (), {})
    star_module.Context = Context
    star_module.Star = Star
    star_module.StarTools = StarTools
    star_module.register = register
    components.Plain = Plain
    api.llm_tool = event.filter.llm_tool
    api.star = star_module
    api.message_components = components
    astrbot.api = api

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star_module,
            "astrbot.api.message_components": components,
        }
    )
    return registry


def _load_plugin(data_dir: Path):
    """按包路径加载 ``main.py``（相对导入才能工作）。"""
    registry = _install_stubs(data_dir)
    package = types.ModuleType("plugin_under_test")
    package.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules["plugin_under_test"] = package
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module, registry


class _FakeSender:
    def __init__(self, user_id: str, nickname: str = "") -> None:
        self.user_id = user_id
        self.nickname = nickname or user_id


class _FakeMessage:
    def __init__(self, **kwargs) -> None:
        self.type = kwargs.get("type", "FriendMessage")
        self.session_id = kwargs.get("session_id", "")
        self.group_id = kwargs.get("group_id", "")
        self.sender = _FakeSender(kwargs.get("sender_id", "10001"), kwargs.get("sender_name", "某某"))
        self.group = types.SimpleNamespace(group_name=kwargs.get("group_name", ""))


class _FakeEvent:
    def __init__(self, umo: str = "aiocqhttp:FriendMessage:10001", **kwargs) -> None:
        self.unified_msg_origin = umo
        self.message_obj = _FakeMessage(**kwargs)
        self._group_id = kwargs.get("group_id", "")
        self._extras = kwargs.get("extras", {})

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname


class _FakeCronManager:
    """cron_manager 替身：记录 delete / add 的调用序（§0#2 的重建纪律）。"""

    def __init__(self, existing=()) -> None:
        self.existing = list(existing)
        self.deleted: list[str] = []
        self.basic: list[dict] = []
        self.active: list[dict] = []

    async def list_jobs(self, job_type=None):
        return list(self.existing)

    async def delete_job(self, job_id: str) -> None:
        self.deleted.append(job_id)

    async def add_basic_job(self, **kwargs):
        self.basic.append(kwargs)
        return SimpleNamespace(job_id="new-basic")

    async def add_active_job(self, **kwargs):
        self.active.append(kwargs)
        return SimpleNamespace(job_id="new-active")


class PluginCase(TmpDirCase):
    module = None
    registry: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.module, cls.registry = _load_plugin(Path.cwd())

    def make_plugin(self, config: dict | None = None, data_dir: Path | None = None, context=None):
        target = data_dir or (self.root / "data")
        target.mkdir(parents=True, exist_ok=True)
        self.module.StarTools = sys.modules["astrbot.api.star"].StarTools
        sys.modules["astrbot.api.star"].StarTools.data_dir = target
        return self.module.DeniaDiary(context=context, config=config or {})


class RegistrationTest(PluginCase):
    def test_nine_tools_are_registered(self) -> None:
        self.assertEqual(
            set(self.registry["tools"]),
            {
                "diary_write",
                "diary_read",
                "diary_list",
                "diary_edit",
                "note_add",
                "note_list",
                "note_complete",
                "note_forget",
                "mood_report",
            },
        )

    def test_no_affinity_tools_are_registered(self) -> None:
        """她不碰熟悉度数值（决策 #5）：不注册任何 affinity_* 工具。"""
        self.assertFalse(
            [name for name in self.registry["tools"] if "affinity" in name],
            "熟悉度没有工具入口",
        )

    def test_plugin_metadata_is_registered(self) -> None:
        name, _author, _desc, version = self.registry["plugin"]
        self.assertEqual(name, "astrbot_plugin_denia_diary")
        self.assertTrue(version)

    def test_hook_is_registered_with_explicit_priority(self) -> None:
        hooks = [h for h in self.registry["hooks"] if h[0] == "on_llm_request"]
        self.assertEqual(len(hooks), 1, "只允许一个提示挂载点")
        self.assertEqual(hooks[0][3].get("priority"), self.module.PROMPT_PRIORITY)
        confirms = [h for h in self.registry["hooks"] if h[0] == "on_using_llm_tool"]
        self.assertEqual(len(confirms), 1, "只允许一个主动消息确认点")

    def test_tool_docstrings_declare_typed_args(self) -> None:
        """AstrBot 在注册期用 docstring_parser 解析 Args，缺类型或类型不支持会直接报错。"""
        for name, func in self.registry["tools"].items():
            doc = func.__doc__ or ""
            params = [
                param
                for param in _signature_params(func)
                if param not in ("self", "event")
            ]
            self.assertTrue(doc.strip(), f"{name} 缺描述")
            for param in params:
                match = re.search(rf"^\s*{param}\((\w+)\):", doc, re.MULTILINE)
                self.assertIsNotNone(match, f"{name} 的 {param} 没写类型注释")
                assert match is not None
                self.assertIn(match.group(1), SUPPORTED_TYPES, f"{name}.{param} 类型不支持")

    def test_tool_descriptions_carry_behavior_rules(self) -> None:
        """工具描述是行为规范（不许写成工作汇报 / 头行不动 / 只能改最近这些天）。"""
        write_doc = self.registry["tools"]["diary_write"].__doc__ or ""
        edit_doc = self.registry["tools"]["diary_edit"].__doc__ or ""
        read_doc = self.registry["tools"]["diary_read"].__doc__ or ""
        self.assertIn("工作汇报", write_doc)
        self.assertIn("开头那行不动", edit_doc)
        self.assertIn("备份", edit_doc)
        self.assertIn("恋爱日记", read_doc)

    def test_notebook_tool_descriptions_carry_behavior_rules(self) -> None:
        """小本本三道闸的第一道：描述里写明只记两类 + 闲聊情绪玩笑不记。"""
        add_doc = self.registry["tools"]["note_add"].__doc__ or ""
        list_doc = self.registry["tools"]["note_list"].__doc__ or ""
        self.assertIn("闲聊", add_doc)
        self.assertIn("不记", add_doc)
        self.assertIn("本子满了", add_doc)
        self.assertIn("不念内容", list_doc)


def _signature_params(func) -> list[str]:
    code = func.__code__
    return list(code.co_varnames[: code.co_argcount])


class ToolFlowTest(PluginCase):
    def test_write_then_read_then_list(self) -> None:
        async def scenario() -> tuple[str, str, str]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            written = await plugin.diary_write(event, text="今天写了一条\n第二段", mood="开心")
            read = await plugin.diary_read(event, tail=5)
            listing = await plugin.diary_list(event, limit=7)
            return written, read, listing

        written, read, listing = asyncio.run(scenario())
        self.assertIn("写好了", written)
        self.assertIn("今天第 1 段", written)
        self.assertIn("今天写了一条", read)
        self.assertIn("〔和某某〕", read)
        self.assertIn("2026" if "2026" in listing else "-", listing)
        self.assertIn("本子共 1 段", listing)

    def test_edit_rewrite_and_delete(self) -> None:
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            await plugin.diary_write(event, text="第一版正文")
            listing = await plugin.diary_edit(event, action="list")
            rewritten = await plugin.diary_edit(event, action="rewrite", match="第一版", text="第二版正文")
            return listing, rewritten

        listing, rewritten = asyncio.run(scenario())
        self.assertIn("共 1 段", listing)
        self.assertIn("改好了", rewritten)
        self.assertIn("恋爱日记" if "恋爱日记" in rewritten else "日记", rewritten)

    def test_missing_segment_reports_hint(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin()
            event = _FakeEvent()
            await plugin.diary_write(event, text="只有这一条")
            return await plugin.diary_edit(event, action="rewrite", match="根本没写", text="x")

        self.assertIn("没找到", asyncio.run(scenario()))

    def test_tools_tolerate_missing_arguments(self) -> None:
        """AstrBot 的 spec_to_func 不生成 ``required``：模型可以不带任何参数调用。

        所以每个工具参数都必须有默认值，缺参数时返回人话而不是 TypeError
        （真机验证时从 AstrBot 自己生成的 JSON schema 里发现：``required`` 是 None）。
        """

        async def scenario() -> tuple:
            plugin = self.make_plugin()
            event = _FakeEvent()
            return (
                await plugin.diary_write(event),
                await plugin.diary_read(event),
                await plugin.diary_list(event),
                await plugin.note_add(event),
                await plugin.note_list(event),
                await plugin.note_complete(event),
                await plugin.note_forget(event),
                await plugin.mood_report(event),
            )

        results = asyncio.run(scenario())
        self.assertIn("没写成", results[0])
        for text in results:
            self.assertIsInstance(text, str)

    def test_disabled_subsystem_blocks_everything(self) -> None:
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin({"subsystems": {"diary": False}})
            event = _FakeEvent()
            return (
                await plugin.diary_write(event, text="写不进去"),
                await plugin.diary_read(event),
            )

        written, read = asyncio.run(scenario())
        self.assertIn("没写成", written)
        self.assertIn("已关闭", read)

    def test_data_dir_from_config_wins(self) -> None:
        custom = self.root / "custom"
        custom.mkdir(parents=True, exist_ok=True)
        plugin = self.make_plugin({"data_dir": str(custom)})
        self.assertEqual(plugin.layout.base_dir, custom)
        self.assertTrue((custom / "日记.txt").parent.is_dir())

    def test_data_dir_defaults_to_host_plugin_dir(self) -> None:
        target = self.root / "host"
        target.mkdir(parents=True, exist_ok=True)
        plugin = self.make_plugin(data_dir=target)
        self.assertEqual(plugin.layout.base_dir, target)


class NotebookToolFlowTest(PluginCase):
    """小本本经真实 ``main.py`` 工具链路的适配器测试（第 2 步）。"""

    def test_add_list_complete_forget_roundtrip(self) -> None:
        async def scenario() -> tuple[str, str, str, str, str]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            added = await plugin.note_add(event, kind="promise", text="帮他查资料", due="2026-10-12")
            note_id = plugin.notebook.store.read()["promises"][0]["id"]
            listing = await plugin.note_list(event)
            completed = await plugin.note_complete(event, note_id=note_id)
            forgotten = await plugin.note_forget(event, note_id=note_id)
            return added, listing, completed, forgotten, note_id

        added, listing, completed, forgotten, note_id = asyncio.run(scenario())
        self.assertIn("记好了", added)
        self.assertIn("期限 2026-10-12", added)
        self.assertIn(f"[{note_id}]", listing)
        self.assertIn("帮他查资料", listing)
        self.assertIn("销账", completed)
        self.assertIn("删掉了", forgotten)

    def test_fact_flow_and_file_shape(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin()
            event = _FakeEvent()
            return await plugin.note_add(event, kind="fact", text="喜欢香菜")

        added = asyncio.run(scenario())
        self.assertIn("记好了", added)
        plugin = self.module.DeniaDiary(context=None, config={})
        raw = json.loads(plugin.layout.notebook.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(raw["facts"]["10001"][0]["text"], "喜欢香菜")
        self.assertEqual(raw["promises"], [])
        self.assertEqual(raw["trash"], [])

    def test_group_fact_injects_in_person_private_only(self) -> None:
        """群里记下的（about 缺省＝发言者）→ 该人私聊注入得到，群里与别人私聊都注入不到。

        第 7 步起默认档下群聊不工作，群聊读写要用 ``all`` 档才验得到——可见性规则本身没变。
        """
        async def scenario() -> tuple[str, str, str]:
            plugin = self.make_plugin({"scope": {"mode": "all"}})
            group_event = _FakeEvent(
                "aiocqhttp:GroupMessage:555",
                type="GroupMessage",
                session_id="555",
                group_id="555",
                sender_id="10001",
            )
            added = await plugin.note_add(group_event, kind="fact", text="爱吃香菜")
            provider = sys.modules["astrbot.api.provider"]
            req_private = provider.ProviderRequest()
            await plugin.inject_diary_hint(_FakeEvent(), req_private)
            req_group = provider.ProviderRequest()
            await plugin.inject_diary_hint(group_event, req_group)
            req_other = provider.ProviderRequest()
            other = _FakeEvent(
                "aiocqhttp:FriendMessage:20002",
                type="FriendMessage",
                session_id="20002",
                sender_id="20002",
            )
            await plugin.inject_diary_hint(other, req_other)
            return added, req_private.system_prompt, req_group.system_prompt + "|" + req_other.system_prompt

        added, private_prompt, others_prompt = asyncio.run(scenario())
        self.assertIn("记好了", added)
        self.assertIn("爱吃香菜", private_prompt)
        self.assertIn("【小本本】", private_prompt)
        self.assertNotIn("爱吃香菜", others_prompt)
        self.assertNotIn("【小本本】关于", others_prompt)

    def test_promise_injection_private_full_group_masked(self) -> None:
        """约定：私聊出全文；群聊只出脱敏计数行（群聊注入要在 ``all`` 档下才发生，第 7 步）。"""
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin({"scope": {"mode": "all"}})
            event = _FakeEvent()
            await plugin.note_add(event, kind="promise", text="帮他带书")
            group_event = _FakeEvent(
                "aiocqhttp:GroupMessage:555",
                type="GroupMessage",
                session_id="555",
                group_id="555",
                sender_id="10001",
            )
            provider = sys.modules["astrbot.api.provider"]
            req_private = provider.ProviderRequest()
            await plugin.inject_diary_hint(event, req_private)
            req_group = provider.ProviderRequest()
            await plugin.inject_diary_hint(group_event, req_group)
            return req_private.system_prompt, req_group.system_prompt

        private_prompt, group_prompt = asyncio.run(scenario())
        self.assertIn("帮他带书", private_prompt)
        self.assertIn("答应过的事", group_prompt)
        self.assertNotIn("帮他带书", group_prompt)

    def test_note_add_reports_full_notebook_message(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin({"notebook": {"fact_limit": 1}})
            event = _FakeEvent()
            await plugin.note_add(event, kind="fact", text="第一条")
            return await plugin.note_add(event, kind="fact", text="第二条")

        self.assertIn("本子满了", asyncio.run(scenario()))

    def test_notebook_disabled_blocks_tools_and_injection(self) -> None:
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin({"subsystems": {"notebook": False}})
            event = _FakeEvent()
            added = await plugin.note_add(event, kind="fact", text="写不进去")
            provider = sys.modules["astrbot.api.provider"]
            req = provider.ProviderRequest()
            await plugin.inject_diary_hint(event, req)
            return added, req.system_prompt

        added, prompt = asyncio.run(scenario())
        self.assertIn("已关闭", added)
        self.assertNotIn("小本本", prompt)


class PromptInjectionTest(PluginCase):
    def test_injection_appends_diary_line_to_system_prompt(self) -> None:
        async def scenario():
            plugin = self.make_plugin()
            # ⚠️ 必须钉死时钟：这句状态词**凌晨和白天不一样**（D8：00:00~05:59 是
            # "新的一天刚开头，还没写"）。以前这条走真挂钟，跑在凌晨就会红——
            # 和"23:30~06:30 必红"那条是同一类坑。
            plugin._now = lambda: NOW
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            req.system_prompt = "你是她。"
            await plugin.inject_diary_hint(event, req)
            return req.system_prompt

        text = asyncio.run(scenario())
        self.assertTrue(text.startswith("你是她。"))
        self.assertIn("【日记】", text)
        self.assertIn("今天还没写", text)

    def test_injection_respects_budget(self) -> None:
        async def scenario() -> int:
            plugin = self.make_plugin()
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            await plugin.inject_diary_hint(event, req)
            return len(req.system_prompt.strip("\n"))

        self.assertLessEqual(asyncio.run(scenario()), self.module.compose.PROMPT_BUDGET)

    def test_injection_silent_when_diary_and_state_disabled(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin({"subsystems": {"diary": False, "state": False}})
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            req.system_prompt = "你是她。"
            await plugin.inject_diary_hint(event, req)
            return req.system_prompt

        self.assertEqual(asyncio.run(scenario()), "你是她。")

    def test_injection_keeps_state_lines_when_diary_disabled(self) -> None:
        """子系统开关互相独立：日记关了，状态（熟悉度档位词）照注。"""

        async def scenario() -> str:
            plugin = self.make_plugin({"subsystems": {"diary": False}})
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            req.system_prompt = "你是她。"
            await plugin.inject_diary_hint(event, req)
            return req.system_prompt

        text = asyncio.run(scenario())
        self.assertTrue(text.startswith("你是她。"))
        self.assertIn("陌生", text, "刚 touch 过一次的人是陌生档")
        self.assertNotIn("【日记】", text)

    def test_injection_records_affinity_touch(self) -> None:
        """钩子先记一次互动：私聊 touch 过后 affinity.json 里有这个人。"""

        async def scenario() -> dict:
            plugin = self.make_plugin({"subsystems": {"diary": False}})
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            await plugin.inject_diary_hint(event, req)
            await plugin.affinity.store.flush()  # 合并写：断言文件前先把窗口冲掉
            return json.loads(
                (plugin.layout.affinity).read_text(encoding="utf-8")
            )

        doc = asyncio.run(scenario())
        self.assertIn("10001", doc["people"])
        self.assertAlmostEqual(doc["people"]["10001"]["score"], 1.0)

    def test_lifecycle_methods_are_noop_safe(self) -> None:
        async def scenario() -> None:
            plugin = self.make_plugin()
            await plugin.initialize()
            await plugin.terminate()

        asyncio.run(scenario())


class MoodToolFlowTest(PluginCase):
    """mood_report 与 diary_write 的情绪沉淀（第 3 步）。"""

    def test_mood_report_writes_state_json(self) -> None:
        async def scenario() -> tuple[str, dict]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            reply = await plugin.mood_report(event, word="有点烦", valence="-1", arousal="-1")
            doc = json.loads(plugin.layout.state.read_text(encoding="utf-8"))
            return reply, doc

        reply, doc = asyncio.run(scenario())
        self.assertIn("记下了", reply)
        self.assertIn("有点烦", reply)
        mood = doc["mood"]
        self.assertEqual(mood["word"], "有点烦")
        self.assertEqual(mood["valence"], -1)
        self.assertEqual(mood["arousal"], -1)
        self.assertIn("updated_at", mood)
        self.assertIn("baseline", mood)
        self.assertEqual(doc["schema_version"], 1)

    def test_mood_report_without_anything_is_a_gentle_error(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin()
            return await plugin.mood_report(_FakeEvent())

        self.assertIn("什么都没收到", asyncio.run(scenario()))

    def test_mood_report_tolerates_garbage_scores(self) -> None:
        async def scenario() -> tuple[str, dict]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            reply = await plugin.mood_report(event, word="还行", valence="abc", arousal="9")
            doc = json.loads(plugin.layout.state.read_text(encoding="utf-8"))
            return reply, doc

        reply, doc = asyncio.run(scenario())
        self.assertIn("记下了", reply)
        mood = doc["mood"]
        self.assertEqual(mood["word"], "还行")
        self.assertEqual(mood["valence"], 0)  # 解析不了＝没给，坐标没动
        self.assertEqual(mood["arousal"], 2)  # 越界夹取

    def test_diary_write_sinks_mood_and_score(self) -> None:
        async def scenario() -> tuple[str, dict]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            reply = await plugin.diary_write(event, text="今天很好", mood="开心", valence="2")
            doc = json.loads(plugin.layout.state.read_text(encoding="utf-8"))
            return reply, doc

        reply, doc = asyncio.run(scenario())
        self.assertIn("写好了", reply)
        mood = doc["mood"]
        self.assertEqual(mood["word"], "开心")
        self.assertEqual(mood["valence"], 2)

    def test_diary_write_ignores_bad_score_but_still_writes(self) -> None:
        async def scenario() -> tuple[str, bool]:
            plugin = self.make_plugin()
            event = _FakeEvent()
            reply = await plugin.diary_write(event, text="心情复杂", valence="很差")
            return reply, plugin.layout.state.exists()

        reply, state_exists = asyncio.run(scenario())
        self.assertIn("写好了", reply, "打分坏了不能连累日记")
        self.assertFalse(state_exists, "没什么可沉淀的就不建 state.json")


class ScopeGateTest(PluginCase):
    """启用范围总闸（第 7 步）：范围外**整轮不介入**——不注入、不写盘、不观察。

    判定住在 core.scope；这里只证明接线正确：默认 private 档下群聊三件写工具全拒、
    注入与联系人不留痕；owner 档连别人的私聊也拒；all 档群聊放行。
    """

    def group_event(self) -> "_FakeEvent":
        return _FakeEvent(
            "aiocqhttp:GroupMessage:555",
            type="GroupMessage",
            session_id="555",
            group_id="555",
            sender_id="10001",
        )

    @staticmethod
    def _snapshot(plugin) -> dict:
        def read(path):
            return path.read_text(encoding="utf-8") if path.exists() else ""

        return {
            "notebook": read(plugin.layout.notebook),
            "diary": read(plugin.layout.diary),
            "love": read(plugin.layout.love_diary),
            "state": read(plugin.layout.state),
            "proactive": read(plugin.layout.proactive),
            "affinity": read(plugin.layout.affinity),
        }

    def test_group_write_tools_are_refused_by_default_and_write_nothing(self) -> None:
        async def scenario():
            plugin = self.make_plugin()
            before = self._snapshot(plugin)
            note = await plugin.note_add(self.group_event(), kind="fact", text="爱吃香菜")
            diary = await plugin.diary_write(self.group_event(), text="群里的日记")
            mood = await plugin.mood_report(self.group_event(), word="有点烦")
            return before, self._snapshot(plugin), (note, diary, mood)

        before, after, answers = asyncio.run(scenario())
        for answer in answers:
            self.assertIn("不在启用范围内", answer)
        self.assertEqual(before, after, "范围外的拒绝一个字节都不落盘")

    def test_group_injection_is_silent_and_leaves_no_trace(self) -> None:
        async def scenario():
            plugin = self.make_plugin()
            provider = sys.modules["astrbot.api.provider"]
            req = provider.ProviderRequest()
            await plugin.inject_diary_hint(self.group_event(), req)
            raw = (
                plugin.layout.proactive.read_text(encoding="utf-8")
                if plugin.layout.proactive.exists()
                else "{}"
            )
            return req.system_prompt, json.loads(raw or "{}")

        prompt, proactive = asyncio.run(scenario())
        self.assertEqual(prompt, "", "范围外不注入")
        self.assertEqual(proactive.get("contacts") or {}, {}, "note_contact 也不记范围外的会话")

    def test_private_injection_still_works_by_default(self) -> None:
        async def scenario():
            plugin = self.make_plugin()
            provider = sys.modules["astrbot.api.provider"]
            req = provider.ProviderRequest()
            await plugin.inject_diary_hint(_FakeEvent(), req)
            return req.system_prompt

        self.assertTrue(asyncio.run(scenario()), "默认档只挡群聊，私聊照常工作")

    def test_owner_mode_refuses_private_strangers_but_serves_master(self) -> None:
        plugin = self.make_plugin({"love_peers": ["10001"], "scope": {"mode": "owner"}})
        stranger = _FakeEvent(
            "aiocqhttp:FriendMessage:20002", type="FriendMessage", session_id="20002", sender_id="20002"
        )
        self.assertIn("不在启用范围内", asyncio.run(plugin.mood_report(stranger, word="平静")))
        self.assertIn("记下了", asyncio.run(plugin.mood_report(_FakeEvent(), word="平静")))

    def test_all_mode_allows_group_writes(self) -> None:
        plugin = self.make_plugin({"scope": {"mode": "all"}})
        self.assertIn("记好了", asyncio.run(plugin.note_add(self.group_event(), kind="fact", text="爱吃香菜")))


class ProactiveCronTest(PluginCase):
    """第 4 步：巡检 job 重建（§0#2）/ 唤醒派发 / 确认点 / 暗号直发。"""

    def setUp(self) -> None:
        super().setUp()
        StarTools = sys.modules["astrbot.api.star"].StarTools
        StarTools.sent = []
        if hasattr(StarTools, "send_result"):
            del StarTools.send_result

    def _manager(self, existing=()) -> _FakeCronManager:
        return _FakeCronManager(existing)

    def _seed_due_promise(self, plugin) -> None:
        asyncio.run(
            plugin.notebook.note_add(
                plugin._session(_FakeEvent()),
                kind="promise",
                text="周三考试",
                due_at="2026-10-04",
                now=plugin._now(),
            )
        )

    def test_initialize_deletes_old_job_and_adds_new(self) -> None:
        """§0#2：按 name 找到旧 job → delete → add 新建；别人的 job 不动。"""
        manager = self._manager(
            [
                SimpleNamespace(name=self.module.PROACTIVE_JOB_NAME, job_id="old-mine"),
                SimpleNamespace(name="someone-else", job_id="old-other"),
            ]
        )
        plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
        asyncio.run(plugin.initialize())
        self.assertEqual(manager.deleted, ["old-mine"])
        self.assertEqual(len(manager.basic), 1)
        job = manager.basic[0]
        self.assertEqual(job["cron_expression"], "*/15 * * * *")
        self.assertFalse(job["persistent"])
        self.assertEqual(job["handler"], plugin._proactive_patrol)

    def test_initialize_without_cron_manager_is_safe(self) -> None:
        plugin = self.make_plugin(context=None)
        asyncio.run(plugin.initialize())  # 不抛即过（宿主裁剪了 cron 模块也不拖垮插件）

    def test_timezone_change_rebuilds_patrol_job(self) -> None:
        """第 11 步回归：job 上的 ``timezone`` 是**建的时候**那个，判定侧却读实时的
        ``settings.zone()`` —— 改了时区不重建，就成了"调度按旧时区、判断按新时区"。
        这条只钉"重建了、且 job 上是新值"；``*/N`` 的触发时刻本来就与时区无关，
        别据此宣称"修掉了一个严重 bug"。
        """
        manager = self._manager([SimpleNamespace(name=self.module.PROACTIVE_JOB_NAME, job_id="old-1")])
        plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
        raw = dict(plugin.config)
        raw["timezone"] = "UTC"
        result = asyncio.run(plugin.apply_settings(raw, applied=["timezone"]))
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["reloaded"], "改了时区必须重建巡检 job")
        self.assertEqual(manager.deleted, ["old-1"], "旧 job 要先删掉，别留僵尸")
        self.assertEqual(len(manager.basic), 1)
        self.assertEqual(manager.basic[0]["timezone"], "UTC", "job 上要挂新时区，不能两套真相")
        self.assertTrue(any("时区" in item for item in result["notices"]), result["notices"])

    def test_unrelated_change_does_not_rebuild_patrol_job(self) -> None:
        """对照组：只改日记字数不该动巡检 job（别把重建做成"每次保存都重建"）。"""
        manager = self._manager([SimpleNamespace(name=self.module.PROACTIVE_JOB_NAME, job_id="old-1")])
        plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
        raw = dict(plugin.config)
        raw["diary"] = {**dict(raw.get("diary") or {}), "max_chars": 1500}
        result = asyncio.run(plugin.apply_settings(raw, applied=["diary.max_chars"]))
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["reloaded"], "没改间隔也没改时区，不该重建")
        self.assertEqual(manager.basic, [])
        self.assertEqual(manager.deleted, [])

    def test_patrol_dispatches_active_job_with_session_and_note(self) -> None:
        manager = self._manager()
        plugin = self.make_plugin(context=SimpleNamespace(cron_manager=manager))
        # 这条测的是"派发"，不是免打扰：默认 late_night 是 23:30-06:30，而 `_proactive_patrol()`
        # 走真实挂钟——不钉死的话，这条用例每天 23:30~06:30 必红（第 5 步真机复核时真撞上）。
        plugin.state.is_late_night = lambda **kwargs: False
        self._seed_due_promise(plugin)
        asyncio.run(plugin.proactive.note_contact(plugin._session(_FakeEvent())))
        asyncio.run(plugin._proactive_patrol())
        self.assertEqual(len(manager.active), 1, "唤醒恰好派发一次")
        job = manager.active[0]
        self.assertIsNone(job["cron_expression"], "复核 #1：该参数没有默认值，必须显式传")
        self.assertTrue(job["run_once"])
        payload = job["payload"]
        self.assertEqual(payload["session"], "aiocqhttp:FriendMessage:10001")
        self.assertIn("周三考试", payload["note"])

    def test_patrol_silent_when_subsystem_disabled(self) -> None:
        manager = self._manager()
        plugin = self.make_plugin(
            config={"subsystems": {"proactive": False}},
            context=SimpleNamespace(cron_manager=manager),
        )
        self._seed_due_promise(plugin)
        asyncio.run(plugin.proactive.note_contact(plugin._session(_FakeEvent())))
        asyncio.run(plugin._proactive_patrol())
        self.assertEqual(manager.active, [])

    def test_confirmation_hook_writes_last_sent_at(self) -> None:
        async def scenario() -> dict:
            plugin = self.make_plugin()
            tool = SimpleNamespace(name="send_message_to_user")
            event = _FakeEvent(extras={"cron_job": {"id": "j1"}})
            await plugin.note_proactive_sent(event, tool, {})
            return json.loads(plugin.layout.proactive.read_text(encoding="utf-8"))

        doc = asyncio.run(scenario())
        entry = doc["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertIn("last_sent_at", entry)

    def test_confirmation_ignores_passive_round(self) -> None:
        """被动轮她回复用的也是 send_message_to_user——没带 cron_job extra 就不写。"""
        async def scenario() -> dict:
            plugin = self.make_plugin()
            tool = SimpleNamespace(name="send_message_to_user")
            await plugin.note_proactive_sent(_FakeEvent(), tool, {})
            return plugin.layout.proactive.exists()

        self.assertFalse(asyncio.run(scenario()))

    def test_confirmation_ignores_other_tools(self) -> None:
        async def scenario() -> bool:
            plugin = self.make_plugin()
            tool = SimpleNamespace(name="diary_write")
            event = _FakeEvent(extras={"cron_job": {"id": "j1"}})
            await plugin.note_proactive_sent(event, tool, {})
            return plugin.layout.proactive.exists()

        self.assertFalse(asyncio.run(scenario()))

    def test_signal_send_success_confirms(self) -> None:
        async def scenario() -> dict:
            plugin = self.make_plugin()
            StarTools = sys.modules["astrbot.api.star"].StarTools
            await plugin._send_signal("aiocqhttp:FriendMessage:10001", "。")
            doc = json.loads(plugin.layout.proactive.read_text(encoding="utf-8"))
            chain = StarTools.sent[0][1]
            return doc, chain

        doc, chain = asyncio.run(scenario())
        entry = doc["sessions"]["aiocqhttp:FriendMessage:10001"]
        self.assertIn("last_sent_at", entry)
        self.assertEqual(chain.chain[0].text, "。", "整条消息就是暗号字符本身")

    def test_signal_send_failure_does_not_confirm(self) -> None:
        """复核 #2：send_message 返回 False（平台没找到）绝不能污染互动轨迹。"""
        async def scenario() -> bool:
            plugin = self.make_plugin()
            StarTools = sys.modules["astrbot.api.star"].StarTools
            StarTools.send_result = False
            await plugin._send_signal("aiocqhttp:FriendMessage:10001", "。")
            return plugin.layout.proactive.exists()

        self.assertFalse(asyncio.run(scenario()))

    def test_signal_send_exception_does_not_confirm(self) -> None:
        async def scenario() -> bool:
            plugin = self.make_plugin()
            StarTools = sys.modules["astrbot.api.star"].StarTools

            async def boom(cls, session, chain):
                raise ValueError("StarTools not initialized")

            original = StarTools.send_message
            StarTools.send_message = classmethod(boom)
            try:
                await plugin._send_signal("aiocqhttp:FriendMessage:10001", "。")
            finally:
                StarTools.send_message = original
            if not plugin.layout.proactive.exists():
                return True  # 文件没建过 → 自然没有 last_sent_at
            doc = json.loads(plugin.layout.proactive.read_text(encoding="utf-8"))
            entry = (doc.get("sessions") or {}).get("aiocqhttp:FriendMessage:10001") or {}
            return "last_sent_at" not in entry

        self.assertTrue(asyncio.run(scenario()))

    def test_injection_also_records_contact(self) -> None:
        """on_llm_request 里顺手记录 person → 会话映射（触发器的目标反查表）。"""
        async def scenario() -> dict:
            plugin = self.make_plugin()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            await plugin.inject_diary_hint(_FakeEvent(), req)
            return json.loads(plugin.layout.proactive.read_text(encoding="utf-8"))

        doc = asyncio.run(scenario())
        self.assertEqual(doc["contacts"]["10001"]["kind"], "private")


if __name__ == "__main__":
    unittest.main(verbosity=2)
