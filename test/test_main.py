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
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import NOW, TmpDirCase, private_session  # noqa: E402

from core.diary import card_render  # noqa: E402

SUPPORTED_TYPES = {"string", "number", "boolean", "object", "array"}
MODULE_NAME = "plugin_under_test.main"


class _EnginePatcher:
    """临时替换某个 ``card_render`` 模块实例里的几个引擎。

    第 20 步起出图是"缓存 → playwright → remote → pil"的降级梯，而**这台机器上三条路都可能
    是真的能用的**（装了 Chrome、装了 Pillow、能联网）。不把引擎换掉，测"降级"就会变成
    "看这台机器装了什么"——那样测出来的东西换个环境就是假的。

    换成返回 None 的假实现 = 那条路"确定出不了图"，剩下的交给编排。

    ⚠️ ``module`` 必须传**插件自己用的那个** ``card_render``（``self.module.card_render``）。
    本文件里 ``from core.diary import card_render`` 拿到的是**另一个模块实例**——测试把插件
    装在 ``plugin_under_test`` 包下重载了一遍，两份 ``core.diary.card_render`` 各不相干，
    补丁打在测试这份上，插件那份纹丝不动。踩过一次：断言拿到的还是 PIL 真出的缓存图。
    """

    def __init__(self, module, *names: str) -> None:
        self.module = module
        self.names = names or tuple(module.ENGINE_ORDER)
        self._saved: dict = {}

    def __enter__(self):
        async def dead(*args, **kwargs):
            return None

        for name in self.names:
            self._saved[name] = getattr(self.module, f"render_{name}")
            setattr(self.module, f"render_{name}", dead)
        return self

    def __exit__(self, *exc):
        for name, original in self._saved.items():
            setattr(self.module, f"render_{name}", original)
        self._saved.clear()
        return False


def _all_engines_dead(module) -> _EnginePatcher:
    """三个引擎全换成"出不了图"。"""
    return _EnginePatcher(module)


def _only_engine(module, name: str) -> _EnginePatcher:
    """只留 ``name`` 这一个引擎可用，其余换成"出不了图"。"""
    return _EnginePatcher(module, *[n for n in module.ENGINE_ORDER if n != name])


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

        def on_llm_tool_respond(self, **kwargs):
            return self._record("on_llm_tool_respond", None, kwargs)

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
            # 真实 ProviderRequest 自带这个字段：每轮临时内容，不参与前缀缓存、
            # 标了 temp 的也不落会话历史。main.py 的注入走这里而不是 system_prompt。
            self.extra_user_content_parts: list = []

    class TextPart:
        """``astrbot.core.agent.message.TextPart`` 的最小替身。"""

        def __init__(self, text: str = "") -> None:
            self.text = text
            self.is_temp = False

        def mark_as_temp(self) -> "TextPart":
            self.is_temp = True
            return self

    class _RecordingLogger:
        """``astrbot.api.logger`` 的替身：接口同形，顺便把内容留下来可断言。"""

        def __init__(self) -> None:
            self.records: list[tuple[str, str]] = []

        def _record(self, level: str, msg: object, *args: object) -> None:
            try:
                self.records.append((level, str(msg) % args if args else str(msg)))
            except Exception:  # noqa: BLE001 - 格式化失败也别把测试带崩
                self.records.append((level, str(msg)))

        def debug(self, msg: object, *args: object) -> None:
            self._record("debug", msg, *args)

        def info(self, msg: object, *args: object) -> None:
            self._record("info", msg, *args)

        def warning(self, msg: object, *args: object) -> None:
            self._record("warning", msg, *args)

        def error(self, msg: object, *args: object) -> None:
            self._record("error", msg, *args)

        def exception(self, msg: object, *args: object) -> None:
            self._record("exception", msg, *args)

        def critical(self, msg: object, *args: object) -> None:
            self._record("critical", msg, *args)

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
    core_module = types.ModuleType("astrbot.core")
    agent_module = types.ModuleType("astrbot.core.agent")
    agent_message = types.ModuleType("astrbot.core.agent.message")

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
    agent_message.TextPart = TextPart
    agent_module.message = agent_message
    core_module.agent = agent_module
    api.llm_tool = event.filter.llm_tool
    api.star = star_module
    api.message_components = components
    api.logger = _RecordingLogger()
    astrbot.api = api
    astrbot.core = core_module

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.provider": provider,
            "astrbot.api.star": star_module,
            "astrbot.api.message_components": components,
            "astrbot.core": core_module,
            "astrbot.core.agent": agent_module,
            "astrbot.core.agent.message": agent_message,
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
        # 真实事件带 message_str（纯文本）；早晚安识别读它，默认空＝普通消息
        self.message_str = kwargs.get("message_str", "")

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key: str, value) -> None:
        # 真身是 AstrMessageEvent.set_extra(key, value) → self._extras[key] = value
        self._extras[key] = value

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self.message_obj.sender.user_id

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname


SENT_TOOL_TEXT = "Message sent to session aiocqhttp:FriendMessage:10001"
"""宿主 ``send_message_to_user`` 成功时真正返回的字符串。

``astrbot/core/tools/message_tools.py``：``return f"Message sent to session {target_session}"``。
"""


def _tool_result(text: str):
    """``on_llm_tool_respond`` 收到的**结果对象**的忠实替身。

    真身形状（宿主 ``astr_agent_tool_exec.py`` 的 ``_execute_local``）：``call()`` 的字符串
    返回值被包成 ``mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=...)])``。
    这里照着建同形对象——**判定必须真的读到成功文案**，不许拿裸字符串 / None / ``{}``
    当"成功"糊过去（"读不懂的结果不算成功"另有专门的用例）。
    """

    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], isError=False)


def _sent_result(text: str = SENT_TOOL_TEXT):
    """成功结果（宿主返回 ``Message sent to session ...``）。"""
    return _tool_result(text)


def _failed_result(
    text: str = "error: failed to find platform for session aiocqhttp:FriendMessage:10001.",
):
    """失败结果（平台没找到 → ``sent`` 为假 → 宿主返回 ``error: ...``）。"""
    return _tool_result(text)


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


def _parts_text(req) -> str:
    """注入内容的实际落点（第 13 步）：``extra_user_content_parts`` 各 part 的 text。"""
    return "".join(getattr(part, "text", "") for part in req.extra_user_content_parts)


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
    def test_ten_tools_are_registered(self) -> None:
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
                "sleep_check",
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
        self.assertEqual(len(confirms), 1, "只允许一个发送前改造钩子")
        results = [h for h in self.registry["hooks"] if h[0] == "on_llm_tool_respond"]
        self.assertEqual(len(results), 1, "只允许一个发送后确认钩子")

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

    def test_data_dir_inside_host_dir_wins(self) -> None:
        """配置的 data_dir 只有落在宿主插件数据目录**之内**才生效（第 13 步越界守卫）。"""
        custom = self.root / "data" / "custom"
        custom.mkdir(parents=True, exist_ok=True)
        plugin = self.make_plugin({"data_dir": str(custom)})
        self.assertEqual(plugin.layout.base_dir, custom)
        self.assertTrue((custom / "日记.txt").parent.is_dir())

    def test_data_dir_outside_host_dir_falls_back(self) -> None:
        """越界（宿主目录之外的任何路径）→ 记 warning 并退回宿主目录，不照着写出去。"""
        outside = self.root / "custom"
        outside.mkdir(parents=True, exist_ok=True)
        plugin = self.make_plugin({"data_dir": str(outside)})
        self.assertEqual(plugin.layout.base_dir, self.root / "data")
        records = sys.modules["astrbot.api"].logger.records
        self.assertTrue(
            any(level == "warning" and "data_dir" in message for level, message in records),
            f"越界要留一条 warning：{records!r}",
        )

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
            return added, _parts_text(req_private), _parts_text(req_group) + "|" + _parts_text(req_other)

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
            return _parts_text(req_private), _parts_text(req_group)

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
            return added, _parts_text(req)

        added, prompt = asyncio.run(scenario())
        self.assertIn("已关闭", added)
        self.assertNotIn("小本本", prompt)


class PromptInjectionTest(PluginCase):
    def test_injection_hint_goes_to_extra_parts_not_system_prompt(self) -> None:
        """审核第二条：动态内容不拼 system_prompt（打掉前缀缓存），走临时内容块。"""
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
            return req

        req = asyncio.run(scenario())
        self.assertEqual(req.system_prompt, "你是她。", "system_prompt 必须原样不动")
        text = _parts_text(req)
        self.assertIn("【日记】", text)
        self.assertIn("今天还没写", text)
        self.assertTrue(
            req.extra_user_content_parts[0].is_temp, "提示块要标临时（不进会话历史）"
        )

    def test_injection_respects_budget(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin()
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            await plugin.inject_diary_hint(event, req)
            return _parts_text(req).strip("\n")

        text = asyncio.run(scenario())
        self.assertTrue(text, "应该真的注入了内容")
        self.assertLessEqual(len(text), self.module.compose.PROMPT_BUDGET)

    def test_injection_silent_when_diary_and_state_disabled(self) -> None:
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin({"subsystems": {"diary": False, "state": False}})
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            req.system_prompt = "你是她。"
            await plugin.inject_diary_hint(event, req)
            return req.system_prompt, _parts_text(req)

        system_prompt, injected = asyncio.run(scenario())
        self.assertEqual(system_prompt, "你是她。", "不该碰 system_prompt")
        self.assertEqual(injected, "", "日记和状态都关了就什么都不注")

    def test_injection_keeps_state_lines_when_diary_disabled(self) -> None:
        """子系统开关互相独立：日记关了，状态（熟悉度档位词）照注。"""

        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin({"subsystems": {"diary": False}})
            event = _FakeEvent()
            req = sys.modules["astrbot.api.provider"].ProviderRequest()
            req.system_prompt = "你是她。"
            await plugin.inject_diary_hint(event, req)
            return req.system_prompt, _parts_text(req)

        system_prompt, text = asyncio.run(scenario())
        self.assertEqual(system_prompt, "你是她。", "不碰 system_prompt")
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


class GreetingWiringTest(PluginCase):
    """早晚安接线（钩子 → 记录 → 注入素材 → 主动消息双向防重）。

    时钟钉死在上午 8 点（greeting 的"刚说"窗只有 2 分钟，且凌晨归属不同——
    别让测试撞真挂钟）。"""

    MORNING_NOW = None

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from datetime import datetime
        from zoneinfo import ZoneInfo

        cls.MORNING_NOW = datetime(2026, 10, 11, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def _run_hook(self, plugin, event):
        req = sys.modules["astrbot.api.provider"].ProviderRequest()
        await plugin.inject_diary_hint(event, req)
        return _parts_text(req)

    def test_morning_greeting_recorded_injected_and_slot_marked(self) -> None:
        async def scenario() -> tuple[str, dict, dict]:
            plugin = self.make_plugin()
            plugin._now = lambda: self.MORNING_NOW
            event = _FakeEvent(message_str="早安")
            text = await self._run_hook(plugin, event)
            proactive = json.loads(
                (plugin.layout.proactive).read_text(encoding="utf-8")
            )
            greetings = json.loads(
                (plugin.layout.greeting).read_text(encoding="utf-8")
            )
            return text, proactive, greetings

        text, proactive, greetings = asyncio.run(scenario())
        self.assertIn("刚说了早安", text, "问候素材进注入（trace 档）")
        self.assertIn("连续 1 天", text)
        self.assertEqual(len(greetings["people"]["10001"]["events"]), 1)
        today = self.MORNING_NOW.date().isoformat()
        slots = proactive["sessions"]["aiocqhttp:FriendMessage:10001"]["slots_today"][today]
        self.assertIn("morning", slots, "他先说了早安，她的早安主动问候当天不再发")

    def test_night_greeting_marks_goodnight_slot(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        async def scenario() -> list:
            plugin = self.make_plugin()
            night = datetime(2026, 10, 10, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            plugin._now = lambda: night
            event = _FakeEvent(message_str="晚安")
            text = await self._run_hook(plugin, event)
            doc = json.loads((plugin.layout.proactive).read_text(encoding="utf-8"))
            return text, doc["sessions"]["aiocqhttp:FriendMessage:10001"]["slots_today"]["2026-10-10"]

        text, slots = asyncio.run(scenario())
        self.assertIn("刚道了晚安", text)
        self.assertIn("goodnight", slots)

    def test_non_greeting_message_records_nothing(self) -> None:
        async def scenario() -> tuple[str, bool]:
            plugin = self.make_plugin()
            plugin._now = lambda: self.MORNING_NOW
            event = _FakeEvent(message_str="你好呀，在吗")
            return await self._run_hook(plugin, event), plugin.layout.greeting.exists()

        text, exists = asyncio.run(scenario())
        self.assertNotIn("刚说了早安", text)
        self.assertFalse(exists, "没有问候就不该建记录文件")

    def test_greeting_out_of_scope_not_recorded(self) -> None:
        """范围外整轮不介入（第 7 步）：默认 private 档下群聊的早安不记、不注入。"""
        async def scenario() -> tuple[str, bool]:
            plugin = self.make_plugin()
            plugin._now = lambda: self.MORNING_NOW
            event = _FakeEvent(
                "aiocqhttp:GroupMessage:555",
                type="GroupMessage",
                session_id="555",
                group_id="555",
                sender_id="10001",
                message_str="早安",
            )
            return await self._run_hook(plugin, event), plugin.layout.greeting.exists()

        text, exists = asyncio.run(scenario())
        self.assertFalse(exists, "范围外的问候不留痕")
        self.assertNotIn("刚说了早安", text)

    def test_greeting_disabled_skips_everything(self) -> None:
        async def scenario() -> tuple[str, bool]:
            plugin = self.make_plugin({"subsystems": {"greeting": False}})
            plugin._now = lambda: self.MORNING_NOW
            event = _FakeEvent(message_str="早安")
            return await self._run_hook(plugin, event), plugin.layout.greeting.exists()

        text, exists = asyncio.run(scenario())
        self.assertFalse(exists)
        self.assertNotIn("刚说了早安", text)

    def test_sleep_check_tool_reports_records_in_private(self) -> None:
        async def scenario() -> str:
            plugin = self.make_plugin()
            plugin._now = lambda: self.MORNING_NOW
            await plugin.greeting.observe(private_session("10001"), text="早安", now=self.MORNING_NOW)
            return await plugin.sleep_check(_FakeEvent())

        body = asyncio.run(scenario())
        self.assertIn("TA 的作息", body)
        self.assertIn("早安 8:00", body)
        self.assertIn("连续 1 天道早安", body)

    def test_sleep_check_tool_refuses_group_and_empty(self) -> None:
        async def scenario() -> tuple[str, str]:
            plugin = self.make_plugin()
            plugin._now = lambda: self.MORNING_NOW
            group = _FakeEvent(
                "aiocqhttp:GroupMessage:555",
                type="GroupMessage",
                session_id="555",
                group_id="555",
                sender_id="10001",
            )
            refused = await plugin.sleep_check(group)
            empty = await plugin.sleep_check(_FakeEvent())
            return refused, empty

        refused, empty = asyncio.run(scenario())
        self.assertIn("群里不翻", refused)
        self.assertIn("还没记到", empty, "没记录就明说，别给她编的素材")

    def test_sleep_check_tool_description_carries_behavior_rules(self) -> None:
        doc = self.registry["tools"]["sleep_check"].__doc__ or ""
        self.assertIn("别编", doc)
        self.assertIn("私聊", doc)


class _FakeTTSProvider:
    """宿主 TTS provider 替身：``get_audio(text) -> 路径``，可指路可报错。"""

    def __init__(self, mapping: dict[str, str] | None = None, error: bool = False) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []
        self.error = error

    async def get_audio(self, text: str) -> str:
        self.calls.append(text)
        if self.error:
            raise RuntimeError("tts down")
        return self.mapping.get(text, "")


class SpeechWiringTest(PluginCase):
    """F1 语音出口接线（主动轮 send_message_to_user → 宿主 TTS → record 组件）。"""

    @staticmethod
    def _context(provider) -> SimpleNamespace:
        async def getter():
            return provider

        return SimpleNamespace(get_using_tts_provider_async=getter)

    async def _run_hook(self, plugin, event, tool_args) -> None:
        """发送前改造（on_using_llm_tool）：清洗 + 语音就地改 tool_args。"""
        tool = SimpleNamespace(name="send_message_to_user")
        await plugin.note_proactive_sent(event, tool, tool_args)

    async def _run_confirm(self, plugin, event, tool_args, result=None) -> None:
        """发送后确认（on_llm_tool_respond）：结果说送达了才写轨迹。"""
        tool = SimpleNamespace(name="send_message_to_user")
        await plugin.confirm_proactive_sent(
            event, tool, tool_args, _sent_result() if result is None else result
        )

    def _proactive_event(self) -> "_FakeEvent":
        return _FakeEvent(extras={"cron_job": {"slot": "morning"}})

    def test_voice_mode_replaces_plain_with_record(self) -> None:
        async def scenario() -> tuple[object, dict]:
            provider = _FakeTTSProvider({"早呀，今天也元气满满": "/tmp/voice/a.mp3"})
            plugin = self.make_plugin({"voice": {"mode": "voice"}})
            plugin.context = self._context(provider)
            umo = "aiocqhttp:FriendMessage:10001"
            plugin.proactive._pending_speak[umo] = True  # 巡检决策暂存（_decide_speak 落点）
            tool_args = {"messages": [{"type": "plain", "text": "早呀，今天也元气满满"}]}
            event = self._proactive_event()
            await self._run_hook(plugin, event, tool_args)
            messages = tool_args["messages"]
            # 宿主真的把这条语音送出去了 → 确认点这时才写 last_sent_at
            await self._run_confirm(plugin, event, tool_args)
            doc = json.loads((plugin.layout.proactive).read_text(encoding="utf-8"))
            return messages, doc

        messages, doc = asyncio.run(scenario())
        self.assertEqual(
            messages, [{"type": "record", "path": "/tmp/voice/a.mp3"}], "纯语音档：文字被替换"
        )
        self.assertIn("last_sent_at", doc["sessions"]["aiocqhttp:FriendMessage:10001"],
                      "确认点照常生效（语音不改变两段式）")

    def test_dual_mode_appends_record_after_plain(self) -> None:
        async def scenario() -> list:
            provider = _FakeTTSProvider({"早安": "/tmp/voice/a.mp3"})
            plugin = self.make_plugin({"voice": {"mode": "dual"}})
            plugin.context = self._context(provider)
            plugin.proactive._pending_speak["aiocqhttp:FriendMessage:10001"] = True
            tool_args = {"messages": [{"type": "plain", "text": "早安"}]}
            await self._run_hook(plugin, self._proactive_event(), tool_args)
            return tool_args["messages"]

        self.assertEqual(
            asyncio.run(scenario()),
            [{"type": "plain", "text": "早安"}, {"type": "record", "path": "/tmp/voice/a.mp3"}],
            "dual 档：语音在前文字在后（对齐宿主 dual_output 惯例）",
        )

    def test_off_mode_and_passive_turn_never_touch_messages(self) -> None:
        async def scenario() -> tuple[list, list, list]:
            provider = _FakeTTSProvider({"早安": "/tmp/a.mp3"})
            plugin_off = self.make_plugin({"voice": {"mode": "off"}})
            plugin_off.context = self._context(provider)
            args_off = {"messages": [{"type": "plain", "text": "早安"}]}
            await self._run_hook(plugin_off, self._proactive_event(), args_off)

            plugin_passive = self.make_plugin({"voice": {"mode": "voice"}})
            plugin_passive.context = self._context(provider)
            args_passive = {"messages": [{"type": "plain", "text": "早安"}]}
            tool = SimpleNamespace(name="send_message_to_user")
            # 被动轮（无 cron extra）：宿主管线有自己的 TTS 开关，插件不碰
            await plugin_passive.note_proactive_sent(_FakeEvent(), tool, args_passive)
            return args_off["messages"], args_passive["messages"], provider.calls

        off, passive, calls = asyncio.run(scenario())
        self.assertEqual(off, [{"type": "plain", "text": "早安"}])
        self.assertEqual(passive, [{"type": "plain", "text": "早安"}])
        self.assertEqual(calls, [], "没有决策暂存（off 档/被动轮）就不该调 TTS")

    def test_no_provider_or_synth_failure_keeps_text(self) -> None:
        async def scenario() -> tuple[list, list]:
            plugin_noc = self.make_plugin({"voice": {"mode": "voice"}})
            plugin_noc.context = SimpleNamespace()  # 宿主没有 provider 方法
            plugin_noc.proactive._pending_speak["aiocqhttp:FriendMessage:10001"] = True
            args_noc = {"messages": [{"type": "plain", "text": "早安"}]}
            await self._run_hook(plugin_noc, self._proactive_event(), args_noc)

            provider = _FakeTTSProvider(error=True)
            plugin_err = self.make_plugin({"voice": {"mode": "voice"}})
            plugin_err.context = self._context(provider)
            plugin_err.proactive._pending_speak["aiocqhttp:FriendMessage:10001"] = True
            args_err = {"messages": [{"type": "plain", "text": "早安"}]}
            await self._run_hook(plugin_err, self._proactive_event(), args_err)
            return args_noc["messages"], args_err["messages"]

        no_provider, synth_failed = asyncio.run(scenario())
        self.assertEqual(no_provider, [{"type": "plain", "text": "早安"}])
        self.assertEqual(synth_failed, [{"type": "plain", "text": "早安"}], "红线：语音坏了不能连累她说话")

    def test_decide_speak_respects_mode_probability_and_mood(self) -> None:
        plugin = self.make_plugin({"voice": {"mode": "voice"}})
        plugin.proactive._voice_roll = lambda: 0.0
        self.assertTrue(plugin.proactive._decide_speak(now=plugin._now()))
        plugin.proactive._voice_roll = lambda: 0.99
        self.assertTrue(plugin.proactive._decide_speak(now=plugin._now()),
                        "probability=100 必开口，与 roll 无关")
        plugin_off = self.make_plugin({"voice": {"mode": "off"}})
        self.assertFalse(plugin_off.proactive._decide_speak(now=plugin_off._now()))

    def test_wake_note_tells_her_about_voice(self) -> None:
        """决策=开口 → note 里明说这条是语音（她在写之前就知道，按说话的口吻写）。"""
        from core.proactive.api import VOICE_NOTE

        plugin = self.make_plugin({"voice": {"mode": "voice"}})
        plugin.proactive._voice_roll = lambda: 0.0
        candidate = SimpleNamespace(
            slot="morning",
            umo="aiocqhttp:FriendMessage:10001",
            session=private_session("10001"),
            fragment="作息词",
            instruction="说早安",
            priority=5,
        )
        decision = plugin.proactive._build_decision(
            candidate, dict(plugin.settings.proactive), plugin._now()
        )
        self.assertTrue(decision["speak"])
        self.assertIn("语音", decision["note"])
        self.assertIn(VOICE_NOTE, decision["note"])

    def test_off_mode_wake_note_has_no_voice_note(self) -> None:
        plugin = self.make_plugin()  # voice.mode 默认 off
        candidate = SimpleNamespace(
            slot="morning",
            umo="aiocqhttp:FriendMessage:10001",
            session=private_session("10001"),
            fragment="作息词",
            instruction="说早安",
            priority=5,
        )
        decision = plugin.proactive._build_decision(
            candidate, dict(plugin.settings.proactive), plugin._now()
        )
        self.assertFalse(decision["speak"])
        self.assertNotIn("语音", decision["note"])


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
            return _parts_text(req), json.loads(raw or "{}")

        prompt, proactive = asyncio.run(scenario())
        self.assertEqual(prompt, "", "范围外不注入")
        self.assertEqual(proactive.get("contacts") or {}, {}, "note_contact 也不记范围外的会话")

    def test_private_injection_still_works_by_default(self) -> None:
        async def scenario():
            plugin = self.make_plugin()
            provider = sys.modules["astrbot.api.provider"]
            req = provider.ProviderRequest()
            await plugin.inject_diary_hint(_FakeEvent(), req)
            return _parts_text(req)

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


class SendResultContractTest(PluginCase):
    """``send_result_delivered``：宿主 ``on_llm_tool_respond`` 结果契约的判定（纯函数）。

    只认**正向成功证据**（宿主 ``message_tools.py`` 的 ``"Message sent to session ..."``）；
    "没报错"不算成功——宿主 ``_execute_local`` 包出来的 ``CallToolResult.isError`` 在失败
    时也是默认 ``False``。
    """

    def test_host_success_result_is_delivered(self) -> None:
        self.assertTrue(self.module.send_result_delivered(_sent_result()))

    def test_host_failure_result_is_not_delivered(self) -> None:
        self.assertFalse(self.module.send_result_delivered(_failed_result()))
        self.assertFalse(
            self.module.send_result_delivered(
                _tool_result("error: messages parameter is empty or invalid.")
            )
        )

    def test_is_error_flag_rejects_even_with_success_text(self) -> None:
        result = _sent_result()
        result.isError = True
        self.assertFalse(self.module.send_result_delivered(result))

    def test_none_and_unknown_shapes_are_not_delivered(self) -> None:
        for value in (
            None,
            {},
            [],
            SimpleNamespace(),
            SimpleNamespace(content=None),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="")]),
        ):
            self.assertFalse(
                self.module.send_result_delivered(value), f"不该算送达：{value!r}"
            )

    def test_bare_success_string_is_tolerated(self) -> None:
        """旧形态兜底：宿主若把 ``call()`` 的返回值原样交来（没包 ``CallToolResult``），
        同一个成功标记照样认——但这**不是**宽松：错文案 / 失败文案依然不认。"""
        self.assertTrue(self.module.send_result_delivered(SENT_TOOL_TEXT))
        self.assertFalse(self.module.send_result_delivered("error: nope"))

    def test_mark_matches_host_source(self) -> None:
        """成功标记必须与宿主源码里的文案一字不差（改了这里就等于换了契约）。"""
        host = (
            Path(__file__).resolve().parents[2]
            / "_race" / "a" / "astrbot" / "AstrBot-master"
            / "astrbot" / "core" / "tools" / "message_tools.py"
        )
        if not host.exists():
            self.skipTest("宿主源码快照不在（离线跑测试的正常情况）")
        text = host.read_text(encoding="utf-8")
        self.assertIn(self.module.SENT_TOOL_OK_MARK, text)


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

    # ---- 确认点：宿主结果说送达了才写轨迹（两段式第二步） -------------------------

    CONFIRM_UMO = "aiocqhttp:FriendMessage:10001"

    def _proactive_event(self, umo: str = "aiocqhttp:FriendMessage:10001") -> "_FakeEvent":
        """主动轮事件：宿主只在 cron 唤醒的事件上挂 ``cron_job`` extra。"""
        return _FakeEvent(umo, extras={"cron_job": {"id": "j1"}})

    async def _pre_send(self, plugin, event, tool_args: dict | None = None) -> None:
        """发送前改造钩子（``on_using_llm_tool``）。"""
        tool = SimpleNamespace(name="send_message_to_user")
        await plugin.note_proactive_sent(event, tool, {} if tool_args is None else tool_args)

    async def _respond(self, plugin, event, result, tool_name: str = "send_message_to_user") -> None:
        """发送后确认钩子（``on_llm_tool_respond``）。"""
        tool = SimpleNamespace(name=tool_name)
        await plugin.confirm_proactive_sent(event, tool, {}, result)

    def _seed_decision(self, plugin, slot: str = "promise", fragment: str = "周三考试") -> None:
        """巡检决策暂存（两段式第一步 ``_commit`` 的落点）。"""
        plugin.proactive._pending_log[self.CONFIRM_UMO] = {"slot": slot, "fragment": fragment}
        plugin.proactive._pending_speak[self.CONFIRM_UMO] = True

    def _entry(self, plugin) -> dict:
        if not plugin.layout.proactive.exists():
            return {}
        doc = json.loads(plugin.layout.proactive.read_text(encoding="utf-8"))
        return (doc.get("sessions") or {}).get(self.CONFIRM_UMO) or {}

    def test_pre_send_hook_writes_nothing(self) -> None:
        """**本步回归（这条就是那个 bug）**：``on_using_llm_tool`` 跑在发送**之前**，
        结果还不存在——``last_sent_at`` / 发送记录一个字都不许写。"""
        async def scenario() -> tuple[bool, bool]:
            plugin = self.make_plugin()
            self._seed_decision(plugin)
            await self._pre_send(plugin, self._proactive_event())
            return plugin.layout.proactive.exists(), plugin.layout.proactive_log.exists()

        proactive_exists, log_exists = asyncio.run(scenario())
        self.assertFalse(proactive_exists, "发送前不许写 last_sent_at")
        self.assertFalse(log_exists, "发送前不许追加发送记录")

    def test_success_result_writes_last_sent_at_and_log(self) -> None:
        """宿主返回成功文案 → 确认点落 ``last_sent_at`` + 发送记录（slot/fragment 取暂存）。"""
        async def scenario() -> tuple[dict, list]:
            plugin = self.make_plugin()
            plugin._now = lambda: NOW + timedelta(minutes=5)
            self._seed_decision(plugin)
            await self._respond(plugin, self._proactive_event(), _sent_result())
            return self._entry(plugin), plugin.proactive.store.read_log_lines()

        entry, lines = asyncio.run(scenario())
        self.assertEqual(entry["last_sent_at"], (NOW + timedelta(minutes=5)).isoformat(timespec="seconds"))
        self.assertEqual(len(lines), 1, "一次送达一条记录")
        self.assertEqual(
            {k: lines[0][k] for k in ("umo", "slot", "fragment")},
            {"umo": self.CONFIRM_UMO, "slot": "promise", "fragment": "周三考试"},
        )

    def test_failed_result_writes_nothing(self) -> None:
        """宿主返回 ``error: ...``（平台没找到）→ 不写 ``last_sent_at``、不烧冷却、不记日志。

        配额在决策时已经吃了（两段式第一步），这里是**故意不回滚**：换轨迹不撒谎。
        """
        async def scenario() -> tuple[bool, bool]:
            plugin = self.make_plugin()
            plugin._now = lambda: NOW
            self._seed_decision(plugin, slot="signal", fragment="暗号字符「。」")
            await self._respond(plugin, self._proactive_event(), _failed_result())
            return bool(plugin.proactive.store.read().get("sessions")), plugin.layout.proactive_log.exists()

        proactive_exists, log_exists = asyncio.run(scenario())
        self.assertFalse(proactive_exists, "没送达不许写 last_sent_at / 暗号冷却")
        self.assertFalse(log_exists, "没送达不许进发送记录")

    def test_failure_then_success_records_the_pending_decision(self) -> None:
        """先失败后成功（宿主重试）：失败不消耗决策暂存，成功那次记的是**原来的 slot/fragment**。"""
        async def scenario() -> tuple[dict, list]:
            plugin = self.make_plugin()
            plugin._now = lambda: NOW
            self._seed_decision(plugin, slot="promise", fragment="周三考试")
            event = self._proactive_event()
            await self._respond(plugin, event, _failed_result())
            failed_entry = self._entry(plugin)
            await self._respond(plugin, event, _sent_result())
            return failed_entry, plugin.proactive.store.read_log_lines()

        failed_entry, lines = asyncio.run(scenario())
        self.assertEqual(failed_entry, {}, "失败那一次什么都没写")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["slot"], "promise")
        self.assertEqual(lines[0]["fragment"], "周三考试")

    def test_passive_round_with_success_result_writes_nothing(self) -> None:
        """被动轮她回复用的也是 ``send_message_to_user``（结果同样成功）——没带 ``cron_job``
        extra 就不算她"找过你"。"""
        async def scenario() -> tuple[bool, bool]:
            plugin = self.make_plugin()
            self._seed_decision(plugin)
            await self._respond(plugin, _FakeEvent(), _sent_result())
            return plugin.layout.proactive.exists(), plugin.layout.proactive_log.exists()

        proactive_exists, log_exists = asyncio.run(scenario())
        self.assertFalse(proactive_exists)
        self.assertFalse(log_exists)

    def test_other_tool_with_success_result_writes_nothing(self) -> None:
        """同一轮里别的工具成功，不是"她发出了主动消息"。"""
        async def scenario() -> tuple[bool, bool]:
            plugin = self.make_plugin()
            self._seed_decision(plugin)
            await self._respond(
                plugin, self._proactive_event(), _sent_result(), tool_name="diary_write"
            )
            return plugin.layout.proactive.exists(), plugin.layout.proactive_log.exists()

        proactive_exists, log_exists = asyncio.run(scenario())
        self.assertFalse(proactive_exists)
        self.assertFalse(log_exists)

    def test_unreadable_result_is_not_success(self) -> None:
        """**读不懂的结果一律不算成功**（不许拿宽松桩当成功）：``None``（工具没有返回值 /
        走直接发送通道）、空 content、文案里没有成功标记、根本不是结果对象。"""
        cases = [
            None,
            SimpleNamespace(content=[]),
            SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")]),
            SimpleNamespace(content=[SimpleNamespace(type="image", data="x")]),
            SimpleNamespace(content="error: whatever"),
            {},
        ]

        async def scenario() -> list[dict]:
            written = []
            for index, result in enumerate(cases):
                plugin = self.make_plugin(data_dir=self.root / f"case{index}")
                self._seed_decision(plugin)
                await self._respond(plugin, self._proactive_event(), result)
                written.append(self._entry(plugin))
            return written

        entries = asyncio.run(scenario())
        self.assertEqual(entries, [{} for _ in cases], f"都不该写轨迹：{entries!r}")

    def test_duplicate_callback_records_only_once(self) -> None:
        """重复回调（宿主重放同一个成功结果 / 她一轮连调两次）：``last_sent_at`` 不被二次改写，
        发送记录只加一行——记录是"她这次找过你"的记账，不是平台消息条数。"""
        async def scenario() -> tuple[dict, list, str]:
            plugin = self.make_plugin()
            self._seed_decision(plugin)
            event = self._proactive_event()
            plugin._now = lambda: NOW
            await self._respond(plugin, event, _sent_result())
            first = self._entry(plugin)["last_sent_at"]
            plugin._now = lambda: NOW + timedelta(minutes=9)  # 第二次回调时间明显不同
            await self._respond(plugin, event, _sent_result())
            return self._entry(plugin), plugin.proactive.store.read_log_lines(), first

        entry, lines, first = asyncio.run(scenario())
        self.assertEqual(entry["last_sent_at"], first, "重复回调不许改写 last_sent_at")
        self.assertEqual(entry["last_sent_at"], NOW.isoformat(timespec="seconds"))
        self.assertEqual(len(lines), 1, "重复回调不许加第二条发送记录")
        records = sys.modules["astrbot.api"].logger.records
        self.assertTrue(
            any(level == "info" and "重复回调" in message for level, message in records),
            f"重复回调要留一条可排查的日志：{records!r}",
        )

    def test_consecutive_tool_calls_in_one_turn_record_once(self) -> None:
        """一轮里调了两次工具（两次真实发送）：仍然只记一条——一次唤醒 = 一条记录。"""
        async def scenario() -> list:
            plugin = self.make_plugin()
            self._seed_decision(plugin)
            event = self._proactive_event()
            await self._respond(plugin, event, _sent_result())
            await self._respond(plugin, event, _sent_result())
            return plugin.proactive.store.read_log_lines()

        self.assertEqual(len(asyncio.run(scenario())), 1)

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
            return bool(plugin.proactive.store.read().get("sessions"))

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


class DiaryCardTest(PluginCase):
    """/看日记 指令：注册、降级链、群聊口径、日期规整。

    handler 是 async generator：用 _collect 把 yield 的结果收成列表再断言。
    html_render / text_to_image 在用例里用实例属性顶掉基类方法——
    桩里的 Star 基类没有这两个方法，天然等价于"渲染器不可用"。
    """

    def _collect(self, coro):
        async def run_all():
            out = []
            async for item in coro:
                out.append(item)
            return out

        return asyncio.run(run_all())

    def _event(self, **kwargs):
        class _CardEvent(_FakeEvent):
            def __init__(self) -> None:
                super().__init__(**kwargs)
                self.plains: list = []
                self.images: list = []

            def plain_result(self, text):
                self.plains.append(str(text))
                return ("plain", text)

            def image_result(self, path):
                self.images.append(str(path))
                return ("image", path)

        return _CardEvent()

    def test_commands_are_registered_with_aliases_and_docstrings(self) -> None:
        commands = {
            name: (kwargs, func)
            for kind, name, func, kwargs in self.registry["hooks"]
            if kind == "command"
        }
        self.assertEqual(set(commands), {"看日记", "日记款式"})
        self.assertEqual(commands["看日记"][0].get("alias"), {"日记卡"})
        self.assertEqual(
            commands["日记款式"][0].get("alias"), {"日记卡样式", "换日记款式"}
        )
        for name, (_kwargs, func) in commands.items():
            self.assertTrue((func.__doc__ or "").strip(), f"{name} 缺文档字符串")

    def test_date_helpers(self) -> None:
        module = self.module
        today = module.datetime(2026, 10, 7)
        self.assertEqual(module._normalize_card_date("", today), "2026-10-07")
        self.assertEqual(module._normalize_card_date("10-06", today), "2026-10-06")
        self.assertEqual(module._normalize_card_date("2-3", today), "2026-02-03")
        self.assertEqual(module._normalize_card_date("2026-10-06", today), "2026-10-06")
        self.assertIsNone(module._normalize_card_date("2-30", today))
        self.assertIsNone(module._normalize_card_date("昨天", today))

    def test_book_name_and_markdown_fallback_content(self) -> None:
        module = self.module
        mixed = [{"book": "love", "time": "09:00", "mood": "", "who": "", "paragraphs": ["a"]},
                 {"book": "normal", "time": "21:00", "mood": "开心", "who": "希", "paragraphs": ["b"]}]
        self.assertEqual(module._card_book_name(mixed), "日记")
        self.assertEqual(module._card_book_name(mixed[:1]), "恋爱日记")
        text = module._card_markdown("2026-10-06", "星期二", "日记", mixed)
        self.assertIn("2026-10-06 星期二", text)
        self.assertIn("**21:00**（开心） 和 希", text)
        self.assertIn("b", text)

    def test_off_sends_text_only(self) -> None:
        plugin = self.make_plugin({"diary": {"card_render": "off"}})
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="只发文本", mood="开心", now=NOW))
        event = self._event()
        results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "plain")
        self.assertIn("只发文本", event.plains[0])
        self.assertEqual(event.images, [])

    def test_pretty_renders_html_and_escapes_content(self) -> None:
        plugin = self.make_plugin({"diary": {"card_engine": "remote", "card_allow_remote": True}})
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="正文 <b>与 & 符号</b>", mood="开心", now=NOW))
        captured: dict = {}

        async def fake_render(tmpl, data, return_url=True, options=None):
            captured["tmpl"] = tmpl
            captured["data"] = data
            return str(self.root / "card.png")

        plugin.html_render = fake_render
        event = self._event()
        results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(results[0][0], "image")
        self.assertIn("card.png", event.images[0])
        self.assertIn("paper 款", captured["tmpl"])  # 默认款式选中的是 paper 模板
        entry = captured["data"]["entries"][0]
        self.assertNotIn("<b>", entry["paragraphs"][0])
        self.assertIn("&lt;b&gt;", entry["paragraphs"][0])

    def test_pretty_falls_back_to_markdown_when_render_fails(self) -> None:
        """**三个引擎全挂**才降 Markdown——第 20 步起出图有 playwright / remote / pil 三条路。

        以前只要 ``html_render`` 挂了就降级；现在 PIL 是保底，那台机器装了 Pillow 就会把图
        接住。所以"验降级"必须**三个都弄挂**，否则测到的根本不是降级，是 PIL 正常工作。
        """
        plugin = self.make_plugin({"diary": {"card_allow_remote": True}})
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="降级也要出图", now=NOW))

        async def broken_render(tmpl, data, return_url=True, options=None):
            raise RuntimeError("All endpoints failed")

        async def broken(*args, **kwargs):
            return None

        plugin.html_render = broken_render
        with _all_engines_dead(self.module.card_render):
            async def fake_t2i(text):
                return str(self.root / "md.png")

            plugin.text_to_image = fake_t2i
            event = self._event()
            results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(results[0][0], "image")
        self.assertIn("md.png", event.images[0])
        self.assertEqual(event.plains, [])

    def test_pretty_falls_back_to_plain_text_when_both_renderers_fail(self) -> None:
        plugin = self.make_plugin()
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="最后还有纯文本", now=NOW))

        async def broken(*args, **kwargs):
            raise RuntimeError("no renderer")

        plugin.html_render = broken
        plugin.text_to_image = broken
        with _all_engines_dead(self.module.card_render):
            event = self._event()
            results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "plain")
        self.assertIn("最后还有纯文本", event.plains[0])

    def test_pil_engine_serves_the_card_when_remote_is_down(self) -> None:
        """远端挂了就该由 PIL 把图接住，而不是直接掉到 Markdown。

        这是第 20 步加 PIL 的全部意义：**出图有一条永远在的底**。
        """
        from core.diary import card_pil

        ok, reason = card_pil.probe()
        if not ok:
            self.skipTest(f"这台机器不能 PIL 出图：{reason}")
        plugin = self.make_plugin({"diary": {"card_engine": "remote"}})
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="PIL 接住了", mood="开心", now=NOW))

        async def broken_render(tmpl, data, return_url=True, options=None):
            raise RuntimeError("All endpoints failed")

        plugin.html_render = broken_render
        with _only_engine(self.module.card_render, "pil"):
            event = self._event()
            results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(results[0][0], "image")
        self.assertTrue(Path(event.images[0]).is_file())
        self.assertEqual(event.plains, [], "出图成功了就不该再补一句纯文本")

    def test_group_gets_hint_without_content(self) -> None:
        plugin = self.make_plugin()
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="私事", now=NOW))
        event = self._event(umo="aiocqhttp:GroupMessage:123456", group_id="123456")
        results = self._collect(plugin.diary_card(event, "10-04"))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("私聊", event.plains[0])
        self.assertNotIn("私事", event.plains[0])

    def test_bad_date_gets_usage_hint(self) -> None:
        plugin = self.make_plugin()
        event = self._event()
        results = self._collect(plugin.diary_card(event, "昨天"))
        self.assertEqual(results[0][0], "plain")
        self.assertIn("/看日记", event.plains[0])

    # ---- 第 15 步：出图参数 / 款式 / 自定义字体 --------------------------------

    def _render_once(self, config: dict | None = None) -> tuple[str, dict]:
        """跑一次 /看日记 的 pretty 档，返回 ``(模板原文, 传给渲染器的数据)``。

        ⚠️ 引擎必须钉死在 ``remote``：第 20 步起出图是"缓存 → playwright → remote → pil"的
        降级梯，而这台机器上 playwright 与 pil **都是真的能用的**。不钉死的话这些用例会去跑
        真浏览器/真 Pillow，``fake_render`` 永远不被调用——测试就变成"看这台机器装了什么"。
        钉死 remote 才是"验那条我自己写的分支"。
        """
        merged = {"diary": {"card_engine": "remote", "card_allow_remote": True, **(config or {}).get("diary", {})}}
        merged.update({k: v for k, v in (config or {}).items() if k != "diary"})
        plugin = self.make_plugin(merged)
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="正文", mood="开心", now=NOW))
        captured: dict = {}

        async def fake_render(tmpl, data, return_url=True, options=None):
            captured.update(tmpl=tmpl, data=data, options=options)
            return str(self.root / "card.png")

        plugin.html_render = fake_render
        self._collect(plugin.diary_card(self._event(), "10-04"))
        return captured["tmpl"], captured["data"]

    def test_render_options_override_the_host_defaults(self) -> None:
        """宿主的默认是 JPEG q40——那正是"字糊成一团"的来源，必须显式覆盖。"""
        _tmpl, _data = self._render_once()
        plugin = self.make_plugin({"diary": {"card_engine": "remote", "card_allow_remote": True}})
        asyncio.run(plugin.diary.write_async(
            private_session("10001"), text="x", now=NOW))
        seen: dict = {}

        async def fake_render(tmpl, data, return_url=True, options=None):
            seen["options"] = options
            return str(self.root / "card.png")

        plugin.html_render = fake_render
        self._collect(plugin.diary_card(self._event(), "10-04"))
        self.assertEqual(seen["options"], {"type": "png", "quality": 95})

    def test_every_declared_style_has_a_template(self) -> None:
        """枚举里有的款式，模板目录里必须真有那个文件（否则静默走降级链出丑图）。"""
        from core import settings as settings_mod

        for style in settings_mod.CARD_STYLE_CHOICES:
            path = self.module.CARD_TEMPLATE_DIR / f"diary_card_{style}.html.j2"
            self.assertTrue(path.is_file(), f"缺模板：{path.name}")

    def test_style_selection_reaches_the_renderer(self) -> None:
        tmpl, _data = self._render_once({"diary": {"card_style": "seal"}})
        self.assertIn("seal 款", tmpl)  # 选中的是 night 那张模板
        self.assertIn("card_font_css", tmpl)  # 每个模板都留了字体插槽

    def test_card_who_survives_unconfigured_names(self) -> None:
        """生产标注是「和某某」不是 id：raw_name 查不到也**不能抹成空串**（1.2.0 起的 P1）。

        write_async 落盘的标注是 ``和某某``（label 的「和」前缀）、person_id=10001；
        没配称呼时卡上的名字回落为标注剥「和」——模板自己会补「和」。
        """
        _tmpl, data = self._render_once()
        self.assertEqual(data["entries"][0]["who"], "某某")

    def test_date_appears_once_in_every_template(self) -> None:
        """日期只在右上角。页脚再抄一遍是冗余（旧版就是这么写着，一屏出现两次）。"""
        from core import settings as settings_mod

        for style in settings_mod.CARD_STYLE_CHOICES:
            src = (self.module.CARD_TEMPLATE_DIR / f"diary_card_{style}.html.j2").read_text(
                encoding="utf-8"
            )
            self.assertEqual(src.count("{{ date }}"), 1, f"{style} 模板里日期出现多次")

    def test_empty_font_leaves_the_style_slot_blank(self) -> None:
        _tmpl, data = self._render_once()
        self.assertEqual(data["card_font_css"], "")

    def test_font_family_is_injected(self) -> None:
        _tmpl, data = self._render_once({"diary": {"card_font": "Noto Serif SC, serif"}})
        css = data["card_font_css"]
        self.assertIn("font-family:Noto Serif SC, serif", css)
        self.assertIn("var(--card-font-stack)", css)  # 换不掉要能回落

    def test_font_url_becomes_a_font_face(self) -> None:
        _tmpl, data = self._render_once(
            {"diary": {"card_font": "https://cdn.example.com/f/x.woff2"}}
        )
        css = data["card_font_css"]
        self.assertIn("@font-face", css)
        self.assertIn("https://cdn.example.com/f/x.woff2", css)

    def test_hostile_font_value_is_dropped(self) -> None:
        """这段 CSS 是**原样**内联的（不像正文那样转义），必须白名单式挡死。"""
        for evil in (
            "</style><script>alert(1)</script>",
            "javascript:alert(1)",
            "Noto Serif SC; } body{display:none",
            "http://evil/x.exe",
        ):
            _tmpl, data = self._render_once({"diary": {"card_font": evil}})
            self.assertEqual(data["card_font_css"], "", f"没挡住：{evil}")


# ---- 第 15 步：/日记款式 换款式 ---------------------------------------------

    def _style_cmd(self, plugin, arg: str = ""):
        """跑 /日记款式，返回 ``(结果列表, 回执全文)``。"""
        event = self._event()
        results = self._collect(plugin.diary_card_style(event, arg))
        return results, "\n".join(event.plains)

    def _fake_apply(self, plugin, sink: list[dict], *, ok: bool = True):
        from core import settings as settings_mod

        async def apply_settings(updated, *, applied=None):
            sink.append(updated)
            if ok:
                plugin.settings = settings_mod.load_settings(updated)
            return {"ok": ok, "applied": ["diary.card_style"], "error": "" if ok else "配置文件写不进去"}

        plugin.apply_settings = apply_settings

    def test_style_command_lists_options_when_called_bare(self) -> None:
        from core import settings as settings_mod

        plugin = self.make_plugin()
        results, text = self._style_cmd(plugin)
        self.assertEqual(results[0][0], "plain")
        for style in settings_mod.CARD_STYLE_CHOICES:
            self.assertIn(style, text)

    def test_style_command_accepts_chinese_alias(self) -> None:
        plugin = self.make_plugin()
        sink: list[dict] = []
        self._fake_apply(plugin, sink)
        _results, text = self._style_cmd(plugin, "手帐")
        self.assertEqual(sink[0]["diary"]["card_style"], "tape")
        self.assertIn("手帐", text)

    def test_style_command_switches_to_another_style(self) -> None:
        from core import settings as settings_mod

        plugin = self.make_plugin()
        sink: list[dict] = []
        self._fake_apply(plugin, sink)
        self._style_cmd(plugin, "seal")
        self.assertEqual(sink[0]["diary"]["card_style"], "seal")
        self.assertEqual(plugin._card_cfg()[1], "seal")
        self.assertEqual(settings_mod.load_settings(sink[0]).diary["card_style"], "seal")

    def test_style_command_keeps_other_diary_settings(self) -> None:
        """换款式只动 card_style，别把 max_chars 之类的一并抹成默认。"""
        plugin = self.make_plugin({"diary": {"max_chars": 3000}})
        sink: list[dict] = []
        self._fake_apply(plugin, sink)
        self._style_cmd(plugin, "dots")
        self.assertEqual(sink[0]["diary"]["max_chars"], 3000)
        self.assertEqual(sink[0]["diary"]["card_style"], "dots")

    def test_style_command_reports_unknown_style_without_changing(self) -> None:
        """认不出的款式要**明说**，不能静默回落默认值——那正是文本框时代的毛病。"""
        plugin = self.make_plugin()
        sink: list[dict] = []
        self._fake_apply(plugin, sink)
        _results, text = self._style_cmd(plugin, "不存在的款")
        self.assertEqual(sink, [], "认不出就不该落盘")
        self.assertIn("没有", text)

    def test_style_command_says_so_when_already_current(self) -> None:
        plugin = self.make_plugin()
        sink: list[dict] = []
        self._fake_apply(plugin, sink)
        _results, text = self._style_cmd(plugin, "paper")   # 默认就是 paper
        self.assertEqual(sink, [])
        self.assertIn("已经", text)

    def test_style_command_reports_save_failure(self) -> None:
        plugin = self.make_plugin()
        sink: list[dict] = []
        self._fake_apply(plugin, sink, ok=False)
        _results, text = self._style_cmd(plugin, "ink")
        self.assertIn("写不进去", text)

    def test_style_alias_table_has_no_unknown_targets(self) -> None:
        """别名表指向的键必须都在枚举里——写错一个就是"输入正确却切不过去"。"""
        from core import settings as settings_mod

        choices = set(settings_mod.CARD_STYLE_CHOICES)
        for alias, target in self.module._CARD_STYLE_ALIASES.items():
            self.assertIn(target, choices, f"别名「{alias}」指向了不存在的款式 {target}")
        for style in choices:
            self.assertIn(style, self.module._CARD_STYLE_LABELS, f"{style} 没有中文名")


class LegacyBackfillWiringTest(PluginCase):
    """方案甲接线：插件把 name_preference（+联系人表）接进 store 的导入回填。"""

    def test_wired_lookup_resolves_from_preference(self) -> None:
        plugin = self.make_plugin({"name_preference": {"10001": "希"}})
        self.assertEqual(plugin._legacy_person_id("和希"), "10001")
        self.assertEqual(plugin._legacy_person_id("和陌生人"), "")
        self.assertEqual(plugin._legacy_person_id("群·某某群"), "")

    def test_wired_lookup_survives_broken_contacts(self) -> None:
        """联系人表坏了不能带累导入——preference 还能命中的照常命中。"""
        plugin = self.make_plugin({"name_preference": {"10001": "希"}})

        class Boom:
            def contacts(self) -> dict:
                raise RuntimeError("坏了")

        plugin.proactive.store = Boom()
        self.assertEqual(plugin._legacy_person_id("和希"), "10001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
