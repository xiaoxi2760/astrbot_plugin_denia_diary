"""日记卡**出图链路的编排**：缓存 → 三引擎降级梯（第 20 步）。

## 为什么要单独一个模块

"先查哪条路、失败了退到哪条"是**判断**，不是翻译。按仓库的分层规矩（``main.py`` 只做翻译
不做判断，见 ``DEVELOPMENT.md``），它得住 ``core/``。住这里还有一个直接好处：**离线可测**——
三个引擎都注入假实现就能把整条降级链跑一遍，不用起 AstrBot、不用装 playwright、不用联网。

## 三个引擎，各是什么

| 引擎 | 怎么出图 | 要什么 | 特点 |
| :--- | :--- | :--- | :--- |
| ``playwright`` | 本机 Chrome / Edge 截图 | playwright + 浏览器 | 排版最贴模板；实测 ~0.9 秒且稳定 |
| ``remote`` | AstrBot 官方 t2i | 能联网 | 实测 4~6 秒；官方端点**随机挑**、失败自动重试，抖动会翻倍 |
| ``pil`` | Pillow 直接画 | Pillow + 中文字体 | 不出网、不起浏览器；照纸感模板重画，见 ``card_pil`` |

``diary.card_engine`` 选的是**优先用哪个**；``auto``（默认）＝按 :data:`ENGINE_ORDER` 的顺序
挑第一个可用的。

## 顺序为什么是这个

:data:`ENGINE_ORDER` 同时是**降级梯**：前一个失败就退到下一个。

1. **缓存**在最前（不在梯子里）——命中就等于把"出图"整个跳过，一次读文件。日记是典型的反复翻。
2. **playwright** ——最快、最稳、最贴模板，能用就该用。
3. **remote** ——比 pil 慢一个数量级，但排版是**六个官方样式都能选**的（pil 只画纸感）。
4. **pil** ——放最后不是因为它差，是因为它是**保底**：Pillow 是 AstrBot 自己的依赖、中文字体
   每台机器都有，它失效的概率最低。放在最后 = 平时不打扰，出事时兜住。

## 失败一定降级，而且一定说

选了 ``pil`` 但 PIL 挂了，会继续退——**但每退一步都记一条日志**。理由和"页码数不出来就不出图"
是同一条：一张出不成图的日记卡，对用户等于"今天读不到日记"，比"慢了 3 秒"严重得多。
所以这里不存在"严格引擎"：配置项只决定**先试谁**，不决定"不许退"。

想知道自己实际用的是哪条，看日志（``[diary-card]`` 前缀）或看 ``card_engine`` 相关的 info。

## 不 import astrbot

``remote`` 由调用方注入（本插件是宿主的 ``Star.html_render``），本模块只管调它、用它的返回值。
既能离线测，也不给 ``core/`` 添 astrbot 依赖。
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .._log import logger
from . import card_cache, card_local, card_pil
from .card import RENDER_OPTIONS

RemoteRenderer = Callable[..., Awaitable[str]]
"""出远端图的回调，签名与宿主的 ``Star.html_render`` 一致
（``(tmpl, data, return_url=..., options=...) -> 图片路径``）。测试传假实现。"""

ENGINE_ORDER: tuple[str, ...] = ("playwright", "remote", "pil")
"""**优先顺序 = 降级顺序**：第一个失败就试下一个，一直试到 ``pil``（保底）。

pil 垫底不是因为它慢，是因为它**最不容易失效**（Pillow 是宿主依赖、中文字体系统自带），
而 remote 排它前面是因为 remote 能出全部六款样式、pil 只能画纸感。
"""

_LOG = "diary-card"


def _ladder(engine: str) -> tuple[str, ...]:
    """把配置值变成"要依次试哪几个引擎"。``auto`` 与非法值都走整条梯子。"""
    if engine in ENGINE_ORDER:
        return (engine, *(name for name in ENGINE_ORDER if name != engine))
    return ENGINE_ORDER


def _staged() -> Path:
    """一个随机名的临时 PNG 落点。

    随机名是必需的：同一张卡完全可能正被两个人同时要，固定文件名会让两边的半成品互相
    覆盖（后写的赢，发出去的还是图，但内容和请求不对应）。
    """
    handle, name = tempfile.mkstemp(suffix=card_cache.SUFFIX)
    os.close(handle)
    return Path(name)


def _keep(cache_dir: Path, key: str, data: bytes) -> Path | None:
    """把画好的 PNG 落成一张**能发出去**的文件（优先缓存目录，写不进去退系统临时目录）。

    缓存是加速器不是前提——它坏了最多让下次重新出一遍，不该让这张卡发不出去。
    """
    return card_cache.put(cache_dir, key, data)


# ---- 三个引擎 -----------------------------------------------------------------
#
# ⚠️ 三个都是**公开**函数，且 :func:`render_html` 按**名字**调它们（不是先塞进 dict 再查表）。
# 这样测试可以单独替换某一个引擎来验证降级——而"三个引擎怎么挂"正是这套编排最需要被盯住的地方。


async def render_playwright(
    html_text: str, ctx: dict[str, Any], cache_dir: Path, key: str, remote: RemoteRenderer
) -> Path | None:
    """本机无头浏览器出图（``core.diary.card_local``）。"""
    staged = _staged()
    try:
        await card_local.render(html_text, staged)
        data = staged.read_bytes()
    except card_local.LocalRenderError as error:
        logger.info("[%s] playwright 出图不可用：%s", _LOG, error)
        return None
    except OSError as error:
        logger.warning("[%s] playwright 产物读不出来：%s", _LOG, error)
        return None
    except Exception as error:  # noqa: BLE001 - 出图链路不许把调用方的指令打断
        logger.warning("[%s] playwright 出图异常：%s", _LOG, error)
        return None
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    return _keep(cache_dir, key, data)


async def render_remote(
    html_text: str, ctx: dict[str, Any], cache_dir: Path, key: str, remote: RemoteRenderer
) -> Path | None:
    """走宿主 ``html_render``（远端 t2i）出图。"""
    try:
        path = await remote(html_text, ctx, return_url=False, options=RENDER_OPTIONS)
    except Exception as error:  # noqa: BLE001 - 端点不通/超时都算这条路走不通
        logger.warning("[%s] remote 出图失败：%s", _LOG, error)
        return None
    target = Path(str(path))
    with contextlib.suppress(OSError):  # 缓存写不进去不影响这次发图
        card_cache.store(cache_dir, key, target.read_bytes())
    return target


async def render_pil(
    html_text: str, ctx: dict[str, Any], cache_dir: Path, key: str, remote: RemoteRenderer
) -> Path | None:
    """Pillow 离线出图（保底，见 ``core.diary.card_pil``）。

    **空 ctx 直接不出图**：pil 画的是数据不是 HTML，调用方要是只递了 HTML 没递 ctx，
    画出来会是一张只有抬头和页脚的空白卡。按仓库的老规矩——宁可这条路走不通，
    也别给一张骗人的空白"日记卡"。
    """
    if not (ctx.get("entries") or []):
        logger.warning("[%s] pil 引擎拿不到模板数据（没有 entries），不出图", _LOG)
        return None
    staged = _staged()
    try:
        await card_pil.render(ctx, staged)
        data = staged.read_bytes()
    except card_pil.PilRenderError as error:
        logger.info("[%s] pil 出图不可用：%s", _LOG, error)
        return None
    except OSError as error:
        logger.warning("[%s] pil 产物读不出来：%s", _LOG, error)
        return None
    except Exception as error:  # noqa: BLE001 - 同上
        logger.warning("[%s] pil 出图异常：%s", _LOG, error)
        return None
    finally:
        with contextlib.suppress(OSError):
            staged.unlink()
    return _keep(cache_dir, key, data)


# ---- 编排 ---------------------------------------------------------------------


async def render_html(
    html_text: str,
    ctx: dict[str, Any],
    *,
    cache_dir: Path,
    key: str,
    engine: str,
    remote: RemoteRenderer,
) -> Path | None:
    """把一张日记卡变成 PNG。**缓存命中 → 依次试引擎 → 全不行返回 None**。

    Args:
        html_text: 卡片 HTML。``playwright`` / ``remote`` 用它；``pil`` 用不上（它画 ``ctx``）。
        ctx: 模板数据（``card.card_context`` 的产物）。``remote`` 与 ``pil`` 要它。
            面板「导出为图片」那边以前只有 HTML，第 20 步起由 ``diary_card_payload``
            一并带出 ``ctx``，所以那条链路也能落到 ``pil``。
        cache_dir: 卡片缓存目录（``storage.Layout.card_cache``）。
        key: :func:`card_cache.key_for` 或 :func:`card_cache.key_for_html` 算出的缓存键。
        engine: ``auto`` / ``playwright`` / ``remote`` / ``pil``（语义见 ``core.settings``）。
        remote: 注入的远端出图回调（测试传假实现）。

    Returns:
        一张**存在的** PNG 的路径；三个引擎都出不了图返回 None（调用方负责降级并告知用户）。
    """
    cached = card_cache.resolve(cache_dir, key)
    if cached is not None:
        return cached

    # 每次调用现读模块全局：测试替换掉某个引擎时，这张表就是替换后的那批。
    runners = {"playwright": render_playwright, "remote": render_remote, "pil": render_pil}
    for position, name in enumerate(_ladder(engine)):
        if position:
            # 不是首选的那几步会说话：用户要知道图到底是谁出的、为什么没走他选的那条
            logger.warning("[%s] 降级到 %s 出图（engine=%s）", _LOG, name, engine)
        rendered = await runners[name](html_text, ctx, cache_dir, key, remote)
        if rendered is not None:
            return rendered
    return None


__all__ = [
    "ENGINE_ORDER",
    "RemoteRenderer",
    "render_html",
    "render_pil",
    "render_playwright",
    "render_remote",
]