"""已渲染日记卡的**落盘缓存**（第 20 步）：同一天同一张卡，只出图一次。

## 为什么需要它

无论走本地还是远端出图，``/看日记`` 的成本都压在同一件事上：**把一段 HTML 变成 PNG**。
本地约 0.9 秒、远端 4~6 秒，而日记是典型的**反复翻**——同一天会被翻好几次，改一段再看一次。
缓存把"出图"这件事按内容寻址存下来，命中就是一次本地读文件（毫秒级）。

## 键怎么算

键 = ``sha256(款式 + 模板签名 + 模板数据)``。三样缺一不可：

- **款式**：``paper`` 和 ``ink`` 是两份不同模板，同一天两种图；
- **模板签名**（``mtime_ns`` + 字节数）：改一行 CSS 必须让旧缓存失效，否则改模板看不到效果，
  还会把一张已经"坏掉"的图反复发出去；
- **模板数据**（``card_context`` 的整个 ctx）：正文改一个字、页码变了、日期换了一天，都是不同的图。

ctx 在 ``card.card_context`` 里**已经 html 转义完毕**，它就是渲染器的确切输入——所以直接对
它取哈希，既精确又不必再关心转义规则。

⚠️ 键里**不**含 user / 会话：卡上只印"和某某"，而这个称呼本身已经进 ctx 了。同一天同一份
内容给谁看都是同一张图，不该因为换个人就重出一张。

## 淘汰

缓存目录在数据目录里，跟着备份一起走，所以必须**有上限**。超过 :data:`MAX_FILES` 就按
mtime 从最旧开始删——mtime 在命中时被 :func:`resolve` 刷新，于是"最近真的被翻过"的卡自然
留在最后。删除失败不抛（缓存而已，下次重建）。

## 出错一律当没命中

这个模块的任何 IO 异常都**不往外抛**：缓存是加速器，不是出图的前提。缓存目录写不进去
（只读挂载、权限、磁盘满）最多让每次都重新出图——不能因此让 ``/看日记`` 直接报错。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

MAX_FILES = 60
"""缓存里最多留几张卡。

按"一天翻一次、能往前翻两个月"估，60 张大约够两个月的回看；每张 ~0.5 MB，整目录封顶在
几十 MB 量级。再多就是纯占地方——真要翻更早的日记，重新出图也就一秒。"""

SUFFIX = ".png"
"""只存 PNG——卡片是文字为主的图，转 JPEG 会在小字上出现振铃（这正是远端默认 q40 的老毛病）。"""


def _template_signature(path: Path) -> tuple[int, int]:
    """模板的 ``(mtime_ns, size)``。读不到就返回 ``(-1, -1)``。

    拿不到签名**不**意味着这次别缓存——只是"改模板可能不会立刻失效"，总比不缓存强；
    真出了这种问题，用户改一下款式（键的一部分）就绕开了。
    """
    try:
        stat = path.stat()
    except OSError:
        return (-1, -1)
    return (stat.st_mtime_ns, stat.st_size)


def key_for(style: str, ctx: dict[str, Any], template: Path) -> str:
    """款式 + 模板签名 + 模板数据 → 32 位十六进制键。**纯函数**，不做 IO 以外的事。"""
    payload = json.dumps(
        {"style": style, "sig": _template_signature(Path(template)), "ctx": ctx},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def key_for_html(html_text: str, *, salt: str = "") -> str:
    """**已经渲好的** HTML → 键。给面板「导出为图片」用。

    它拿到的是成品 HTML、没有 ctx 可算，但不需要：正文、日期、页码、款式排版**全在 HTML
    里**，内容本身就是全部。``salt`` 用来在同一段 HTML 走不同出图引擎时分开缓存
    （本地与远端理论上出一致，但真出差异时不该互相顶掉）。
    """
    payload = json.dumps(
        {"salt": salt, "html": html_text},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def path_for(cache_dir: Path, key: str) -> Path:
    """键 → 缓存文件路径（**不保证存在**，也不创建目录）。"""
    return Path(cache_dir) / f"{key}{SUFFIX}"


def resolve(cache_dir: Path, key: str) -> Path | None:
    """命中返回 PNG 路径（并刷新 mtime，让它留在淘汰队列后面），未命中返回 None。

    mtime 刷新失败（只读目录）不影响命中——拿不到就当没刷新。
    """
    target = path_for(cache_dir, key)
    try:
        if not target.is_file() or target.stat().st_size <= 0:
            return None
    except OSError:
        return None
    with contextlib.suppress(OSError):
        os.utime(target, None)
    return target


def store(cache_dir: Path, key: str, data: bytes) -> Path | None:
    """写入缓存并返回路径；任何一步不行就返回 None（**调用方照样能发图**）。

    先写同目录临时文件再 ``os.replace``：出图这件事在同一个进程里跑，不做原子替换的话，
    崩溃瞬间可能留下半张图——而半张 PNG 也是"合法文件"，下次就被当成命中直接发出去了。
    """
    if not data:
        return None
    directory = Path(cache_dir)
    handle = None
    tmp_name = ""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(suffix=".tmp", dir=str(directory))
        with os.fdopen(handle, "wb") as out:
            handle = None  # 已交给 fdopen，close 归它
            out.write(data)
        target = path_for(directory, key)
        os.replace(tmp_name, target)
        tmp_name = ""
    except OSError:
        return None
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    with contextlib.suppress(OSError):
        _prune(directory)
    return target


def put(cache_dir: Path, key: str, data: bytes) -> Path | None:
    """把 PNG 落成一张**可以直接发出去**的文件，返回它的路径。

    与 :func:`store` 的区别只有一个，但这一个区别是本模块存在的理由：``store`` 失败就返回
    None，而调用方（``/看日记``）拿到 None 只能降级到纯文本——**缓存写不进去，不该让她今天
    读不到日记**。所以这里失败时退到系统临时目录再写一份：还是发得出图，只是下次要重新出。

    连临时目录都写不了才返回 None（磁盘满到那份数据都落不下，这时确实没什么可做的了）。
    """
    if not data:
        return None
    target = store(cache_dir, key, data)
    if target is not None:
        return target
    try:
        handle, name = tempfile.mkstemp(suffix=SUFFIX)
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        return Path(name)
    except OSError:
        return None


def _prune(directory: Path) -> None:
    """删到只剩最新的 :data:`MAX_FILES` 张。全程吞异常——淘汰失败只是占地方，不影响出图。"""
    try:
        files = [f for f in directory.glob(f"*{SUFFIX}") if f.is_file()]
        if len(files) <= MAX_FILES:
            return
        files.sort(key=lambda f: f.stat().st_mtime_ns, reverse=True)
        for stale in files[MAX_FILES:]:
            with contextlib.suppress(OSError):
                stale.unlink()
    except OSError:
        return


def read_bytes(path: Path) -> bytes:
    """读缓存文件。读不出来就抛——调用方据此回落"重新出图"，所以这里**不**该返回空串。"""
    return Path(path).read_bytes()


__all__ = [
    "MAX_FILES",
    "SUFFIX",
    "key_for",
    "key_for_html",
    "path_for",
    "put",
    "read_bytes",
    "resolve",
    "store",
]