"""``web_api._web`` 的**宿主契约测试**（2026-10-06 真机踩坑后补）。

为什么单独一个文件：离线单测跑的是**桩**，真机跑的是**宿主**。两者签名一旦不一致，
"离线全绿"对真机就没有任何保证——这一课是拿真机上**所有错误路径全变 500** 换来的::

    真机 4.28.1：error_response(message, *, status_code=400, data=None, headers=None)
    本仓库当时：error_response(msg, 400, endpoint="x")     # status_code 位置参 + 野 kwarg
    → TypeError: unexpected keyword argument 'endpoint'
    → 被 logged_handler 的 except 接住，兜底那句也是同一个错签名 → 一起炸穿
    → 框架只能回 {"status":"error","message":"Internal server error"}

所以这里**照真机签名造一个假宿主**，把 ``_web`` 重新加载一遍再打：只有"调用宿主的
姿势"对了才算过。桩再宽容也掩盖不了这类错。
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
WEB_PY = PLUGIN_DIR / "web_api" / "_web.py"
HANDLERS_PY = PLUGIN_DIR / "web_api" / "handlers.py"

SHIM_KEYWORDS = frozenset({"errors", "problems", "endpoint", "headers"})
"""``_web.error_response`` 除了 ``status_code`` 之外允许出现的关键字（本仓库内部签名）。"""

HOST_CALL_KEYWORDS = frozenset({"status_code", "data", "headers"})
"""**调用宿主的唯一姿势**：``message`` 位置参 + 这三个关键字。"""


class _FakeHost:
    """照真机 4.28.1 签名做的假宿主；顺手把每次调用记下来。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.json_calls: list[dict] = []

    # --- 真机签名（关键字限定，且不接任意 kwarg） ---
    def error_response(self, message, *, status_code=400, data=None, headers=None):
        self.calls.append(
            {"message": message, "status_code": status_code, "data": data, "headers": headers}
        )
        return {"status": "error", "message": message, "data": data, "_host": True}

    def json_response(self, data, status_code=200, headers=None):
        self.json_calls.append({"data": data, "status_code": status_code})
        return {"status": "ok", "data": data, "status_code": status_code, "_host": True}


def _load_shim(host: _FakeHost):
    """把 ``_web.py`` 当成新模块加载，且让它 import 到假宿主（不污染别的测试）。"""
    fake = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.web": types.ModuleType("astrbot.api.web"),
    }
    fake["astrbot"].__path__ = []  # type: ignore[attr-defined]
    fake["astrbot.api"].__path__ = []  # type: ignore[attr-defined]
    for name in ("error_response", "json_response"):
        setattr(fake["astrbot.api.web"], name, getattr(host, name))
    fake["astrbot.api.web"].request = object()

    saved = {name: sys.modules.get(name) for name in fake}
    sys.modules.update(fake)
    try:
        spec = importlib.util.spec_from_file_location("_web_contract_probe", WEB_PY)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class HostContractTest(unittest.TestCase):
    """① 假宿主必须真的窄（否则这个测试自己就是假绿）；② 归一层的调用姿势必须合法。"""

    def test_fake_host_is_actually_narrow(self) -> None:
        """假宿主得跟真机一样会拒绝野 kwarg —— 不然本文件的结论全是空的。"""
        host = _FakeHost()
        with self.assertRaises(TypeError):
            host.error_response("坏", 400, endpoint="settings")
        with self.assertRaises(TypeError):
            host.error_response("坏", 400, errors=[{"path": "x", "error": "y"}])
        host.error_response("好", status_code=400, data={"endpoint": "settings"}, headers=None)
        self.assertEqual(host.calls[-1]["status_code"], 400)

    def test_shim_signature_is_the_documented_one(self) -> None:
        module = _load_shim(_FakeHost())
        params = inspect.signature(module.error_response).parameters
        self.assertEqual(list(params)[:2], ["message", "status_code"])
        kwonly = {name for name, p in params.items() if p.kind is p.KEYWORD_ONLY}
        self.assertEqual(kwonly, SHIM_KEYWORDS)

    def test_shim_survives_real_host_signature(self) -> None:
        """这一条就是真机那次 500 的回归测试。"""
        host = _FakeHost()
        module = _load_shim(host)
        response = module.error_response(
            "第 1 项不合格（共 2 项没通过检查，什么都没改）",
            400,
            endpoint="settings",
            errors=[{"path": "diary.max_chars", "error": "要整数"}, {"path": "timezone", "error": "无效"}],
            problems=["outbound.marker_pattern 不是合法正则"],
        )
        self.assertTrue(response.get("_host"))
        call = host.calls[-1]
        self.assertEqual(call["status_code"], 400)
        self.assertEqual(call["data"]["endpoint"], "settings")
        self.assertEqual(len(call["data"]["errors"]), 2)
        self.assertEqual(len(call["data"]["problems"]), 1)

    def test_shim_only_passes_allowed_keywords_to_host(self) -> None:
        host = _FakeHost()
        module = _load_shim(host)
        module.error_response("坏", 400, endpoint="portrait/upload")
        module.error_response("更坏", 500)
        for call in host.calls:
            self.assertEqual(set(call) - {"message"}, HOST_CALL_KEYWORDS, call)
        self.assertEqual(host.calls[0]["data"], {"endpoint": "portrait/upload"})
        self.assertIsNone(host.calls[1]["data"], "没有附加信息时别发空对象")

    def test_offline_stub_mirrors_the_data_field(self) -> None:
        """离线桩：``data`` 与 ``extra`` 同物，老断言（读 extra）不能失效。"""
        module = _load_shim_offline()
        response = module.error_response("坏", 400, endpoint="portrait", errors=[{"path": "a"}])
        self.assertEqual(response["_stub"], "error_response")
        self.assertEqual(response["status"], 400)
        self.assertIs(response["data"], response["extra"])
        self.assertEqual(response["extra"]["endpoint"], "portrait")
        self.assertEqual(response["extra"]["errors"], [{"path": "a"}])
        self.assertEqual(module.error_response("干干净净", 400)["extra"], {})


def _load_shim_offline():
    """强制走降级分支：让 `import astrbot.api.web` 必然失败。"""
    saved = {name: sys.modules.get(name) for name in ("astrbot", "astrbot.api", "astrbot.api.web")}
    blocker = types.ModuleType("astrbot")
    blocker.__path__ = []  # type: ignore[attr-defined]
    sys.modules["astrbot"] = blocker
    sys.modules.pop("astrbot.api", None)
    sys.modules.pop("astrbot.api.web", None)
    try:
        spec = importlib.util.spec_from_file_location("_web_offline_probe", WEB_PY)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in saved.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class CallSitesTest(unittest.TestCase):
    """静态核对：handler 里的每个 ``error_response(...)`` 都得落在归一层的签名里。"""

    def _calls(self) -> list[ast.Call]:
        tree = ast.parse(HANDLERS_PY.read_text(encoding="utf-8"))
        found: list[ast.Call] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "error_response":
                found.append(node)
        return found

    def test_every_call_site_fits_the_shim(self) -> None:
        calls = self._calls()
        self.assertGreaterEqual(len(calls), 15, "handlers.py 里的 error_response 调用点不该这么少")
        for node in calls:
            with self.subTest(line=node.lineno):
                self.assertLessEqual(len(node.args), 2, f"第 {node.lineno} 行位置参太多")
                self.assertFalse(
                    any(isinstance(arg, ast.Starred) for arg in node.args),
                    f"第 {node.lineno} 行用了 *args——签名对不上时静态看不出来",
                )
                for keyword in node.keywords:
                    self.assertIsNotNone(keyword.arg, f"第 {node.lineno} 行用了 **kwargs")
                    self.assertIn(keyword.arg, SHIM_KEYWORDS, f"第 {node.lineno} 行传了野关键字 {keyword.arg}")
                if len(node.args) == 2:
                    self.assertIsInstance(node.args[1], ast.Constant, f"第 {node.lineno} 行状态码不是字面量")

    def test_logged_handler_fallback_is_legal(self) -> None:
        """兜底那句自己也得合法——真机上它曾跟着一起炸，500 才露出来的。"""
        self.assertIn("服务端处理失败", HANDLERS_PY.read_text(encoding="utf-8"))
        self.assertIn(
            ("message", "status", ("endpoint",)),
            [_shape(node) for node in self._calls()],
            "logged_handler 的兜底没走「message + 位置状态码 + endpoint」这一形",
        )


def _shape(node: ast.Call) -> tuple:
    """调用形状：`(位置参个数标记, (关键字…))`——用来定位某一句具体写法。"""
    return (
        "message",
        "status" if len(node.args) == 2 else "no-status",
        tuple(keyword.arg for keyword in node.keywords),
    )


if __name__ == "__main__":
    unittest.main()
