"""WebUI 静态对账脚本（第 5 步自检，照参考插件那个做）。

**不依赖 astrbot、不依赖浏览器、不装任何东西**——纯文本核对。跑法::

    py tools/webui_selfcheck.py

查这几件事，任意一条不过就非零退出：

1. ``pages/diary/index.html`` 在（没有它，插件页不会被扫到，§0#1）；
2. 资源引用齐全：``index.html`` 引的每个 ``./xxx`` 都真的在页面目录里（§0#12/#13）；
3. **没有 CDN、没有 ``../``、没有绝对路径**（受限 iframe + CSP，§4）；
4. **后端路由表 ↔ 前端 endpoint 双向一一对应**（§11.2——改了一边忘了另一边就在这里钉死）；
5. 前端 endpoint **不以 ``/`` 开头**（§0#11）；
6. 路由表里的 handler 名都能在 ``build_handlers`` 里找到；
7. 新文件**UTF-8 无 BOM + 纯 LF**（§4）。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PAGE_DIR = PLUGIN_DIR / "pages" / "diary"
INDEX = PAGE_DIR / "index.html"
APP_JS = PAGE_DIR / "app.js"
ROUTES_PY = PLUGIN_DIR / "web_api" / "routes.py"
HANDLERS_PY = PLUGIN_DIR / "web_api" / "handlers.py"

NEW_FILES = (
    "core/webui_data.py",
    "core/webui_settings.py",
    "web_api/__init__.py",
    "web_api/_web.py",
    "web_api/routes.py",
    "web_api/handlers.py",
    "test/test_webui_data.py",
    "test/test_webui_settings.py",
    "pages/diary/index.html",
    "pages/diary/app.js",
    "pages/diary/ui.js",
    "pages/diary/style.css",
    "pages/diary/preview-bridge.js",
    "pages/diary/views/overview.js",
    "pages/diary/views/diary.js",
    "pages/diary/views/notebook.js",
    "pages/diary/views/affinity.js",
    "pages/diary/views/proactive.js",
    "pages/diary/views/settings.js",
)

failures: list[str] = []
checked = 0


def check(condition: bool, message: str) -> None:
    global checked
    checked += 1
    if not condition:
        failures.append(message)


def _ast_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def backend_routes() -> dict[str, tuple[str, ...]]:
    """从 ``routes.py`` 读路由表（AST 解析：endpoint → (handler 名, 方法…)）。"""
    for node in _ast_tree(ROUTES_PY).body:
        target = None
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "ROUTES":
            target = node.value
        elif isinstance(node, ast.Assign) and any(
            getattr(item, "id", "") == "ROUTES" for item in node.targets
        ):
            target = node.value
        if target is None:
            continue
        routes: dict[str, tuple[str, ...]] = {}
        for item in target.elts:  # type: ignore[attr-defined]
            if not (isinstance(item, ast.Tuple) and len(item.elts) == 4):
                continue
            endpoint = ast.literal_eval(item.elts[0])
            handler = ast.literal_eval(item.elts[1])
            methods = tuple(str(ast.literal_eval(one)) for one in item.elts[2].elts)
            routes[str(endpoint)] = (str(handler),) + methods
        return routes
    raise AssertionError("routes.py 里找不到 ROUTES 赋值")


def frontend_endpoints() -> dict[str, str]:
    """从 ``app.js`` 的 ``ENDPOINTS`` 字面量表读 endpoint。"""
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"var\s+ENDPOINTS\s*=\s*\{(.*?)\};", source, re.S)
    assert match, "app.js 里找不到 ENDPOINTS 表"
    endpoints: dict[str, str] = {}
    for key, value in re.findall(r"(\w+)\s*:\s*\"([^\"]+)\"", match.group(1)):
        endpoints[key] = value
    return endpoints


def handler_names() -> set[str]:
    """从 ``handlers.py`` 的 ``build_handlers`` 返回表里取 handler 名。"""
    for node in _ast_tree(HANDLERS_PY).body:
        if not isinstance(node, ast.FunctionDef) or node.name != "build_handlers":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Return) or not isinstance(inner.value, ast.Dict):
                continue
            names: set[str] = set()
            for key in inner.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
            return names
    raise AssertionError("handlers.py 里找不到 build_handlers 的返回表")


def main() -> int:
    global checked
    # 1. 页面在不在
    check(INDEX.is_file(), f"缺 {INDEX.relative_to(PLUGIN_DIR)}——没有它插件页不会被扫到（§0#1）")
    if not INDEX.is_file():
        return report()

    html = INDEX.read_text(encoding="utf-8")

    # 2/3. 资源引用：齐全 + 相对 + 无 CDN
    refs = re.findall(r'(?:src|href)="([^"]+)"', html)
    check(bool(refs), "index.html 里一个资源引用都没有")
    for ref in refs:
        if ref.startswith(("http://", "https://", "//")):
            failures.append(f"引了外部地址（禁 CDN）：{ref}")
        elif ref.startswith("/"):
            failures.append(f"资源用了绝对路径（要用相对路径）：{ref}")
        elif ".." in ref:
            failures.append(f"资源用了 ../（页面目录必须自包含，§0#13）：{ref}")
        else:
            target = (PAGE_DIR / ref.split("?")[0]).resolve()
            if not target.is_file():
                failures.append(f"引用的资源不存在：{ref}")
            elif PAGE_DIR.resolve() not in target.parents:
                failures.append(f"资源跑出页面目录了：{ref}")
        checked += 1

    for name in ("app.js", "ui.js", "style.css", "preview-bridge.js"):
        check((PAGE_DIR / name).is_file(), f"页面目录里少文件：{name}")
    for view in ("overview", "diary", "notebook", "affinity", "proactive", "settings"):
        check((PAGE_DIR / "views" / f"{view}.js").is_file(), f"少了视图：views/{view}.js")
    for name in re.findall(r'src="\./views/([^"]+)"', html):
        check((PAGE_DIR / "views" / name.split("?")[0]).is_file(), f"index.html 引的视图不存在：{name}")

    # 4/5. 路由双向一一对应
    routes = backend_routes()
    endpoints = frontend_endpoints()
    check(bool(routes), "后端路由表读出来是空的")
    check(bool(endpoints), "前端 ENDPOINTS 表读出来是空的")
    for value in endpoints.values():
        check(not value.startswith("/"), f"前端 endpoint 不能以 / 开头（§0#11）：{value}")
    back = set(routes)
    front = set(endpoints.values())
    for missing in sorted(back - front):
        failures.append(f"后端有路由、前端没调用：{missing}")
    for extra in sorted(front - back):
        failures.append(f"前端调了 endpoint、后端没注册：{extra}")
    if len(endpoints) != len(front):
        failures.append("前端 ENDPOINTS 表里有重复的 endpoint 值")

    # 6. handler 名对得上
    names = handler_names()
    for endpoint, spec in routes.items():
        check(spec[0] in names, f"路由 {endpoint} 指向的 handler {spec[0]} 在 build_handlers 里没有")

    # 7. 新文件编码与换行
    for relative in NEW_FILES:
        path = PLUGIN_DIR / relative
        if not path.is_file():
            failures.append(f"交付物缺文件：{relative}")
            continue
        raw = path.read_bytes()
        check(not raw.startswith(b"\xef\xbb\xbf"), f"{relative} 带 UTF-8 BOM（要无 BOM）")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            failures.append(f"{relative} 不是合法 UTF-8")
        check(b"\r\n" not in raw, f"{relative} 里有 CRLF（要纯 LF）")
        checked += 1

    # core/ 零 astrbot
    for py in (PLUGIN_DIR / "core").rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")) and "astrbot" in stripped:
                failures.append(f"core/ 里 import 了 astrbot：{py.name} → {stripped}")
            checked += 1

    # 只 import astrbot.api.web 的模块
    for py in (PLUGIN_DIR / "web_api").rglob("*.py"):
        for line in py.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if "astrbot" in stripped and stripped.startswith(("import ", "from ")) and "api.web" not in stripped:
                failures.append(f"web_api 里 import 了非 api.web 的 astrbot 模块：{py.name} → {stripped}")

    # 前端别用构建产物/框架
    for js in sorted(PAGE_DIR.rglob("*.js")):
        text = js.read_text(encoding="utf-8")
        for bad in ("require(", "cdn.", "unpkg", "jsdelivr"):
            if bad in text:
                failures.append(f"{js.relative_to(PAGE_DIR)} 疑似引了外部依赖：{bad}")
        checked += 1

    # main.py 只加了两行注册：import + 调用
    main_py = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    check("register_all(self.context, PLUGIN_NAME, self)" in main_py, "main.py 里没有 register_all 调用")
    check("from .web_api import register_all" in main_py, "main.py 里没有 import register_all")
    for bad in ("register_web_api(", "astrbot.api.web"):
        check(bad not in main_py, f"main.py 里不该出现 {bad}（注册逻辑在 web_api 里）")

    return report()


def report() -> int:
    if failures:
        print("✗ WebUI 自检没过：")
        for item in failures:
            print(f"  - {item}")
        print(f"共 {checked} 项检查，{len(failures)} 项不过。")
        return 1
    print(f"✓ WebUI 自检全过：{checked} 项检查。")
    print("  · 路由表 ↔ 前端 endpoint 双向一一对应")
    print("  · 资源全部相对、齐全、无 CDN、无 ../")
    print("  · core/ 零 astrbot import；web_api 只碰 api.web")
    print("  · 新文件 UTF-8 无 BOM + 纯 LF")
    return 0


if __name__ == "__main__":
    sys.exit(main())
