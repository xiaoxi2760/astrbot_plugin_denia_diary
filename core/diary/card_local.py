"""日记卡的**本地出图**（第 20 步）：把 HTML 交给本机无头浏览器截图，不出网。

## 为什么要有这一条路

``/看日记`` 的精美卡片默认走宿主的 ``html_render``，那是**远端** t2i 服务。实测（本机、
真实卡片负载）两个官方端点各要 **4.4~6.3 秒**，而且宿主每次 ``random.shuffle`` 随机挑一个、
失败还会**自动换下一个重试**——最坏一次出图把等待翻倍。相比之下同一台机器上把同一段 HTML
丢给本机 Chrome 截图只要 **~0.9 秒**，且耗时稳定、不受端点抖动影响。

"远端慢"不等于"本地一定行"——宿主机可能压根没有浏览器。所以这里**只提供能力，不做决策**：
``probe()`` 只负责如实回答"这台机器能不能"，能不能出图由 ``diary.card_engine`` 决定
（见 ``core/settings.py``）。任何一步失败都抛 :class:`LocalRenderError`，调用方降级到远端。

## 零硬依赖

本仓库零第三方依赖是硬约束（见 DEVELOPMENT.md），所以 ``playwright`` **只在真正要出图的
那一刻**才 import，探测不到就当"这台机器不能本地出图"——**不会因为少个包而让插件起不来**。
浏览器也不要求是 playwright 自带的那个：找不到自带 chromium 就退到**系统已装的**
Chrome / Edge（Windows、macOS、Linux 常见路径都找），实在没有就 ``probe()`` 返回 False。

要手工指定浏览器，设环境变量 ``DIARY_CARD_BROWSER`` 指向可执行文件即可（最高优先级）。

## 常驻浏览器

冷启动要 1.4 秒、热渲染只要 0.9 秒，差的那 0.5 秒全是浏览器进程起停。所以浏览器**留在进程里
复用**，首次出图时懒启动，插件卸载时用 :func:`shutdown` 收掉——否则会留下一个没人管的
Chromium 进程。截图完的 page 立刻关掉（每开一个 page 都是一份内存）。

浏览器实例**绑在创建它的事件循环上**，所以它和锁都是模块级单例；万一换��循环，重建即可
（见 :func:`_browser`）。

## 不 import astrbot

和 ``core/`` 其余模块一样：这里只管"把 HTML 变成 PNG 文件"，发不发、发给谁由 ``main.py`` 决定。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

BROWSER_ENV = "DIARY_CARD_BROWSER"
"""手工指定浏览器可执行文件的��境变量名。指向不存在的文件**不算错**——当成"没指定"继续找。"""

VIEWPORT_WIDTH = 800
"""视口宽度（CSS px）。

对齐远端 t2i 的出图宽度（实测 800×1506），这样本地与远端出的两张图**一模一样**——
换引擎不该顺带换掉她看到的排版。"""

DEVICE_SCALE = 1
"""设备像素比。保持 1：2x 更清晰，但体积从 ~0.5 MB 涨到 ~1.5 MB，发 QQ 的上传时间成正比。
卡片一天发一次，清晰度的收益抵不上那一秒。"""

SETTLE_MS = 120
"""截图前的静默等待（毫秒）。

只用来兜动画 / 过渡；真正的"字画完了"靠 :func:`render` 里等 ``document.fonts.ready``，
不等固定时长是字体没加载完时出半截空白的原因。"""

DEFAULT_TIMEOUT_MS = 20_000
"""单次出图超时。超了就当这台机器不行，降级远端——比让 ``/看日记`` 干等强。"""

_BROWSER_CANDIDATES: tuple[str, ...] = (
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    # Linux
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
)
"""按顺序找的浏览器。Chrome 优先于 Edge——两者渲染同一段 CSS 的差异极小，但 Chrome 在
这批模板上的基线更接近远端 t2i（远端跑的就是 Chromium 系）。"""

_LAUNCH_ARGS: tuple[str, ...] = (
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
)
"""起浏览器的固定参数。

⚠️ **不要**加 ``--disable-lcd-text`` / ``--disable-font-subpixel-positioning`` 之类"关抗锯齿"
的开关：卡片全是 15~16px 的中文小字，关掉亚像素会让字缘发虚——那正是远端 q40 JPEG 的老毛病，
不能再在这儿犯一遍。"""

_pw: Any = None
_browser_handle: Any = None
_browser_loop: Any = None
_lock = asyncio.Lock()
"""浏览器单例、它所属的事件循环、串行化用的锁。懒启动，首次出图时才付出 1.4 秒的冷启动。"""


class LocalRenderError(RuntimeError):
    """这台机器没法本地出图（没浏览器 / playwright 装不上 / 超时 / 崩了）。

    **调用方必须捕获它并降级**——这个异常只说明"本地这条路走不通"，不代表出不了图。
    """


def _browsers_root() -> Path:
    """playwright 下载浏览器的根目录。

    ⚠️ **三个平台的位置完全不同**，写死任何一个都会让另外两个平台静默失效——
    踩过一次：这里原本写的是 Windows 的 ``AppData/Local/ms-playwright``，在 Linux 宿主机上
    扫不到，于是 playwright 引擎在那台机器上一直假装"用不了"，静默退到慢 10 倍的远端。
    """
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "") or str(Path.home() / "AppData" / "Local")
        return Path(local) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


_CHROMIUM_RELPATHS: tuple[str, ...] = (
    "chrome-linux64/chrome",            # playwright 现在的 Linux 布局
    "chrome-linux/chrome",              # 旧版布局
    "chrome-headless-shell-linux64/chrome-headless-shell",
    "chrome-win64/chrome.exe",
    "chrome-win/chrome.exe",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
)
"""chromium 在版本目录里的相对位置。**Linux 那个是 ``chrome-linux64`` 不是 ``chrome-linux``**
——实际装出来就是这个名字，写成后者等于找不到（同样踩过）。"""


def _playwright_browser_path() -> Path | None:
    """playwright 自带的 chromium（``playwright install chromium`` 装的那个）。

    探测不到就返回 None——它只是候选之一，找不到不算失败：外层还有系统浏览器兜底，
    再不济 :func:`_ensure_browser` 也会把 ``executable_path`` 整个省掉，交给 playwright
    自己解析它那份权威路径。
    """
    # 按约定直接读目录，**不为拿个路径就起一次浏览器进程**（探测必须便宜）。
    try:
        root = _browsers_root()
        if not root.is_dir():
            return None
        # 目录形如 chromium-1234 / chromium_headless_shell-1234；带版本号，排序取最新
        for pattern in ("chromium-*", "chromium_headless_shell-*"):
            for child in sorted(root.glob(pattern), reverse=True):
                for rel in _CHROMIUM_RELPATHS:
                    candidate = child / rel
                    if candidate.is_file():
                        return candidate
        return None
    except Exception:  # noqa: BLE001 - 探测只读，异常一律当成"没找到"
        return None


def find_browser() -> Path | None:
    """找一个能用的浏览器：环境变量 → playwright 自带 → 系统 Chrome / Edge。找不到返回 None。"""
    raw = os.environ.get(BROWSER_ENV, "").strip()
    if raw:
        # 设了 BROWSER_ENV 就**以它为准**，哪怕那个文件不存在也不回退到候选表。
        # 静默换成另一个浏览器比"按你说的用、但它不存在"更难查——用户以为自己指定了
        # Chromium 版的，出图的却是 Edge。要改回自动探测，把这个变量**删掉**即可。
        candidate = Path(raw)
        return candidate if candidate.is_file() else None
    bundled = _playwright_browser_path()
    if bundled is not None:
        return bundled
    for raw_path in _BROWSER_CANDIDATES:
        candidate = Path(raw_path)
        if candidate.is_file():
            return candidate
    return None


def probe() -> tuple[bool, str]:
    """这台机器能不能本地出图。返回 ``(能不能, 原因)``——**原因用来写日志和面板**。

    只做静态探测（playwright 能不能 import、浏览器文件在不在），**不真起一次浏览器**：
    探测必须便宜，否则每条命令都要先付 1.4 秒才知道自己能不能用。

    ⚠️ 我们自己扫不到浏览器**不等于**不能用：playwright 自带的 chromium 可能装在我们没
    枚举到的布局里（它的目录约定会变，``chrome-linux`` → ``chrome-linux64`` 就是现成的例子）。
    所以扫不到时这里给 True、理由写"交给 playwright 自己解析"，真正的判据留给
    :func:`_ensure_browser` 的启动失败——那时降级链照样接得住。
    """
    try:
        import playwright.async_api  # noqa: F401, PLC0415 - 故意的懒 import
    except Exception as error:  # noqa: BLE001 - 装不上就是不能用，不是错误
        return False, f"没装 playwright（{type(error).__name__}）"
    found = find_browser()
    if found is not None:
        return True, str(found)
    if os.environ.get(BROWSER_ENV, "").strip():
        return False, f"{BROWSER_ENV} 指向的浏览器不存在"
    return True, "playwright 自带的浏览器（路径未枚举到，交给 playwright 自己解析）"


async def _ensure_browser() -> Any:
    """拿到（必要时起）常驻浏览器。调用方**必须**已持有 :data:`_lock`。

    ⚠️ 单例存进 :data:`_browser_handle`，**不叫 ``_browser``**：本函数要是也叫 ``_browser``，
    ``if _browser is not None`` 第一次就会读到**函数自己**（函数对象不是 None），把函数当
    浏览器返回去，调用方一脸 ``'function' object has no attribute 'new_page'``。
    名字分开是这个模块唯一必须记住的规矩。
    """
    global _pw, _browser_handle, _browser_loop  # global 必须在最前面
    loop = asyncio.get_running_loop()
    if _browser_handle is not None:
        # 换过事件循环就重建：playwright 的浏览器对象**绑在创建它的那个循环上**，
        # 拿到别的循环上去用只会得到一串看不懂的 I/O 报错（实测 Windows 上是
        # "I/O operation on closed pipe"）。旧实例反正已经废了，直接丢掉重来——
        # 这也是插件热重载、宿主换 loop 时唯一说得通的行为。
        if _browser_loop is loop:
            return _browser_handle
        _browser_handle = None
        _pw = None
        _browser_loop = None
    from playwright.async_api import async_playwright  # noqa: PLC0415 - 故意的懒 import

    # 扫到就指定；**扫不到就把 executable_path 整个省掉**，让 playwright 用它自己那份
    # 权威解析——它的目录约定会变，我们枚举的候选表永远只是"大概"。真没浏览器的话
    # launch 自己会抛，被下面收敛成 LocalRenderError，降级链接得住。
    found = find_browser()
    if os.environ.get(BROWSER_ENV, "").strip() and found is None:
        raise LocalRenderError(f"{BROWSER_ENV} 指向的浏览器不存在")
    launch_kwargs: dict[str, Any] = {"args": list(_LAUNCH_ARGS)}
    if found is not None:
        launch_kwargs["executable_path"] = str(found)
    driver = await async_playwright().start()
    try:
        _browser_handle = await driver.chromium.launch(**launch_kwargs)
    except BaseException as error:
        # 起浏览器失败要把驱动也收掉，否则 playwright 的子进程会留在后台
        with contextlib.suppress(Exception):
            await driver.stop()
        raise LocalRenderError(f"起浏览器失败：{error}") from error
    _pw = driver
    _browser_loop = loop
    return _browser_handle


async def _drop_browser() -> None:
    """关掉浏览器并清空单例（出错时调用——带着坏掉的实例继续跑只会一路错下去）。

    **换过事件循环就不关**：旧实例属于那个已经结束的循环，在新循环上调 ``close()`` 只会抛
    一串 transport 异常然后在解释器退出时刷一屏 ``closed pipe``。那时的浏览器进程由
    playwright 的驱动一起带走，我们不碰。
    """
    global _pw, _browser_handle, _browser_loop
    same_loop = _browser_loop is asyncio.get_running_loop()
    if _browser_handle is not None and same_loop:
        with contextlib.suppress(Exception):
            await _browser_handle.close()
    _browser_handle = None
    if _pw is not None and same_loop:
        with contextlib.suppress(Exception):
            await _pw.stop()
    _pw = None


async def shutdown() -> None:
    """插件卸载时收掉常驻浏览器，别给用户留一个孤儿 Chromium 进程。"""
    async with _lock:
        await _drop_browser()


async def render(html_text: str, dest: Path, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Path:
    """把一段 HTML 截成 PNG 写到 ``dest``，返回 ``dest``。失败抛 :class:`LocalRenderError`。

    并发调用会在 :data:`_lock` 上排队——共用一个浏览器进程，串行截图不会互相踩。
    """
    ok, reason = probe()
    if not ok:
        raise LocalRenderError(reason)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 先落临时文件再 goto：截图成功才改名，失败不会留下半张图冒充成功产物
    # （下游只认"文件存在"，半张图比没有图更难查）。
    handle, tmp_name = tempfile.mkstemp(suffix=".html", dir=str(dest.parent))
    os.close(handle)
    tmp_html = Path(tmp_name)
    try:
        tmp_html.write_text(html_text, encoding="utf-8")
        async with _lock:
            try:
                browser = await _ensure_browser()
                page = await browser.new_page(
                    viewport={"width": VIEWPORT_WIDTH, "height": 900},
                    device_scale_factor=DEVICE_SCALE,
                )
                try:
                    await page.goto(
                        tmp_html.resolve().as_uri(),
                        wait_until="load",
                        timeout=timeout_ms,
                    )
                    # 自定义字体（card_font）是 @font-face 拉远程 woff2：不等它，标题和日期
                    # 会用回退字体画出来，再晚一步截图就是一张"字体没换成功"的卡。
                    with contextlib.suppress(Exception):
                        await page.evaluate("() => document.fonts.ready")
                    await page.wait_for_timeout(SETTLE_MS)
                    await page.screenshot(path=str(dest), full_page=True, type="png")
                finally:
                    with contextlib.suppress(Exception):
                        await page.close()
            except BaseException:
                # 浏览器崩了 / 超时 / 页面炸了：丢掉这个实例，下次重新起。
                await _drop_browser()
                raise
    except LocalRenderError:
        raise
    except Exception as error:  # noqa: BLE001 - 统一收敛成 LocalRenderError，让调用方好降级
        raise LocalRenderError(f"本地出图失败：{error}") from error
    finally:
        with contextlib.suppress(OSError):
            tmp_html.unlink()
    return dest


__all__ = [
    "BROWSER_ENV",
    "DEVICE_SCALE",
    "VIEWPORT_WIDTH",
    "LocalRenderError",
    "find_browser",
    "probe",
    "render",
    "shutdown",
]