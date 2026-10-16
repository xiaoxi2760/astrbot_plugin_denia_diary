"""日记卡的 **PIL 出图引擎**：纯 Pillow 画，不起浏览器、不出网。

## 它补的是哪一块

出图现在有三种引擎，各有各的适用面：

| 引擎 | 要什么 | 适合 |
| :--- | :--- | :--- |
| playwright | 本机装了 Chrome / Edge **且**装了 playwright | 排版最贴模板，主力 |
| remote | 能连 AstrBot 官方 t2i | 宿主机没浏览器时的退路 |
| **pil（本模块）** | 装了 Pillow **且**系统有中文字体 | 云主机/无浏览器/要完全离线 |

pil 是三者里**依赖最轻、最不容易失效**的那条：Pillow 是 AstrBot 自己的依赖，中文字体每台
Windows / macOS / Linux 都有。所以它排在降级梯**最后**当保底——前面两条都能用的时候不该用它。

## 为什么是"照着纸感重画"而不是"解释 HTML"

模板是 HTML + CSS，CSS 靠 ``flex``、``repeating-linear-gradient``、``border-radius: 999px``、
``letter-spacing``、``feTurbulence`` 纸纹……这些在 PIL 里没有对应物。硬要解释 CSS 等于自己写
一个残缺浏览器，那才是真·零依赖里最容易烂掉的代码。

所以这里做的是**照着 ``diary_card_paper.html.j2`` 的度量与配色，用 Pillow 重画一张同构的卡**：
同一套行高、同一套颜色、同一套信息结构（抬头 / 时间 + 心情 + 和谁 / 正文 / 页脚页码），
连画布宽度都和另外两个引擎对齐到 **800px**。逐像素一致做不到，也不需要——它是**另一个引擎**，
用户是在"三选一"里主动挑它的。

⚠️ 因此：**pil 引擎不认 ``diary.card_style``**，一律画纸感那一版。样式是 HTML 模板的概念，
PIL 没有模板可换。选了非 paper 的款式又用 pil 时，这里记一条 info 说明白——
**静默换一个样式发出去，比明说"这个引擎不适用"更糟**。

## 字体

中文卡片没有中文字体就是一堆豆腐块，所以 ``probe()`` 除了查 Pillow 还得查字体。
候选表覆盖 Windows（雅黑 / 宋体 / 黑体）、macOS（苹方 / 黑体）、Linux（Noto CJK）；
想手工指定设 ``DIARY_CARD_FONT``（字体文件路径）。

## 线程

PIL 是纯 CPU 同步活，放事件循环里会把整个 bot 卡住。这里统一丢进
:func:`asyncio.to_thread`（``card_render._pil`` 调的），和 playwright 那条一样不占着 loop。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
from pathlib import Path
from typing import Any

FONT_ENV = "DIARY_CARD_FONT"
"""手工指定中文字体文件的环境变量。指错了当没设置（和外键 :data:`DIARY_CARD_BROWSER` 同款）。"""

CANVAS_WIDTH = 800
"""画布宽度。对齐另外两个引擎的出图宽度（远端 t2i 实测 800px、playwright 视口 800px）——
三选一时不该顺带换掉她在 QQ 里看到的尺寸。"""

SCALE = 2
"""超采样倍数。PIL 直接按字号画中文边缘会有锯齿，2 倍画完再降采样能让字缘干净；
代价是 PNG 大一圈（~1~2 MB）。"""

_FONT_CANDIDATES: tuple[str, ...] = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑（模板默认字体栈的首选）
    r"C:\Windows\Fonts\msyhbd.ttc",     # 雅黑 Bold：抬头日期 / 时间
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
"""中文字体候选。**按这个顺序**取第一个能用的——雅黑在前，因为模板的默认字体栈
（``"PingFang SC", "Microsoft YaHei", ...``）本来就是它排第一。"""

_BOLD_CANDIDATES: tuple[str, ...] = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
"""粗体候选。找不到就用常规体——**不要**拿合成加粗糊上去，PIL 的 ``stroke_width`` 会把
15px 的中文小字糊成一坨，比不粗难看得多。"""

_LOG = "diary-card-pil"


class PilRenderError(RuntimeError):
    """PIL 这条路走不通（没 Pillow / 没中文字体 / 画崩了）。

    **调用方必须捕获并降级**——和 ``card_local.LocalRenderError`` 同一个约定。
    """


# ---- 度量（全部照抄 diary_card_paper.html.j2，改单位为"CSS px"，画的时候乘 SCALE）----


class M:
    """纸感那张卡的度量表。名字跟 CSS 里的选择器/变量对得上，方便对着模板改。"""

    pad_body_x = 32          # body 左右 padding
    pad_body_y = 40          # body 上下 padding
    sheet_pad_top = 38
    sheet_pad_right = 52
    sheet_pad_bottom = 34
    sheet_pad_left = 96      # 装订线那一条
    lh = 34                  # --lh：正文行高，**也是所有纵向间距的基准**
    head_pad_bottom = 20
    foot_margin_top = 30
    foot_pad_top = 16

    holes_left = 26
    holes_d = 14
    holes_top = 104
    holes_bottom = 104

    size_book = 12.5
    size_date = 21
    size_weekday = 12.5
    size_body = 16
    size_time = 13.5
    size_mood = 11.5
    size_who = 11.5
    size_foot = 11

    track_book = 0.36        # letter-spacing（em）
    track_weekday = 0.14
    track_foot = 0.12
    track_date = 0.05

    meta_gap = 11
    mood_pad_x = 10

    color_page = "#efe7dc"
    color_sheet = "#fffdf8"
    color_ink = "#3c3a38"
    color_date = "#2f2c2a"
    color_book = "#a89a90"
    color_weekday = "#b09a8e"
    color_rule = "#ddd2c0"
    color_line = "#ece3d7"
    color_margin = "#f2bfcd"
    color_hole = "#f7f2ea"
    color_time = "#8a7d73"
    color_mood = "#c2607c"
    color_mood_bg = "#fdeef2"
    color_mood_border = "#f6d9e1"
    color_who = "#b3a69d"
    color_foot = "#bcb0a7"


# ---- 字体 ---------------------------------------------------------------------


def _find(paths: tuple[str, ...]) -> Path | None:
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_file():
            return candidate
    return None


def find_font() -> Path | None:
    """找一个能用的中文字体文件：环境变量优先，否则走候选表。找不到返回 None。

    ⚠️ 设了 :data:`FONT_ENV` 就**以它为准**，哪怕那个文件不存在也不回退到候选表。
    静默换成另一个字体比"按你说的用、但它不存在"更难查——图已经画成另一个样子了，
    而用户完全不知道为什么。要改回自动探测，把这个变量**删掉**即可。
    """
    raw = os.environ.get(FONT_ENV, "").strip()
    if raw:
        candidate = Path(raw)
        return candidate if candidate.is_file() else None
    return _find(_FONT_CANDIDATES)


def find_bold_font() -> Path | None:
    """粗体字体。找不到返回 None——调用方用常规体顶上。"""
    return _find(_BOLD_CANDIDATES)


def probe() -> tuple[bool, str]:
    """这台机器能不能用 PIL 出图。返回 ``(能不能, 原因)``——**绝不抛**。

    和 playwright 那条一样只做静态探测，不真画一张：探测必须便宜。
    """
    try:
        import PIL  # noqa: F401, PLC0415 - 故意的懒 import
        from PIL import ImageFont  # noqa: F401, PLC0415
    except Exception as error:  # noqa: BLE001 - 装不上就是不能用，不是错误
        return False, f"没装 Pillow（{type(error).__name__}）"
    found = find_font()
    if found is None:
        return False, f"找不到中文字体（可设 {FONT_ENV} 指向字体文件）"
    return True, str(found)


# ---- 排版基元 -----------------------------------------------------------------


class _Pen:
    """一支笔：字体、颜色、度量都从这里出，画的时候只管调用。"""

    def __init__(self, scale: int) -> None:
        from PIL import ImageFont

        regular = find_font()
        if regular is None:
            raise PilRenderError("找不到中文字体")
        bold = find_bold_font() or regular
        self.scale = scale
        self._cache: dict[tuple[str, int], Any] = {}
        self._paths = {"r": regular, "b": bold}

    def font(self, size: float, bold: bool = False) -> Any:
        from PIL import ImageFont

        key = ("b" if bold else "r", int(round(size * self.scale)))
        got = self._cache.get(key)
        if got is None:
            got = ImageFont.truetype(str(self._paths[key[0]]), key[1])
            self._cache[key] = got
        return got

    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def width(self, text: str, size: float, bold: bool = False) -> int:
        if not text:
            return 0
        box = self.font(size, bold).getbbox(text)
        return int(box[2] - box[0])

    def height(self, size: float, bold: bool = False) -> int:
        font = self.font(size, bold)
        box = font.getbbox("国Ag")
        return int(box[3] - box[1])

    def draw_text(
        self, draw: Any, xy: tuple[float, float], text: str, size: float, color: str,
        bold: bool = False, track: float = 0.0,
    ) -> None:
        """画一行字。``track`` 是 CSS 的 ``letter-spacing``（em），逐字排出来。

        PIL 的 ``draw.text`` 没有字距参数，而抬头那行 ``0.36em`` 的字距是这张卡的
        气质所在——没它，"普通日记 · 共 6 段"会挤成一团。逐字画比引第三方排版库划算。
        """
        if not text:
            return
        font = self.font(size, bold)
        x, y = xy
        if track <= 0:
            draw.text((x, y), text, font=font, fill=color)
            return
        step = track * size * self.scale
        for ch in text:
            draw.text((x, y), ch, font=font, fill=color)
            x += self.width(ch, size, bold) + step

    def wrap(self, text: str, size: float, max_width: int, bold: bool = False) -> list[str]:
        """按可用宽度折行。

        中文逐字断、拉丁词不断——日记里两种都有（"今天…去了 cafe"），纯逐字断会把 cafe
        劈成 ca / fe。做法：把连续的 ASCII 字母数字当成一个不可拆的词，其余按字断。
        """
        if not text:
            return [""]
        lines: list[str] = []
        current = ""
        for token in _tokens(text):
            for piece in token:
                candidate = current + piece
                if current and self.width(candidate, size, bold) > max_width:
                    lines.append(current.rstrip())
                    current = "" if piece.isspace() else piece
                else:
                    current = candidate
            # 一个词放不下整行时（超长英文），硬拆
            while self.width(current, size, bold) > max_width and len(current) > 1:
                cut = len(current)
                while cut > 1 and self.width(current[:cut], size, bold) > max_width:
                    cut -= 1
                lines.append(current[:cut])
                current = current[cut:]
        lines.append(current)
        return [line or "" for line in lines] or [""]


def _tokens(text: str) -> list[str]:
    """切成"不可拆的词"：连续 ASCII 字母数字算一个，其余每字一个。"""
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "._/@+-"):
            buf += ch
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
    if buf:
        out.append(buf)
    return out


# ---- 画图 ---------------------------------------------------------------------


def _layout(ctx: dict[str, Any], pen: _Pen) -> dict[str, Any]:
    """只算不画。高度必须先算准，否则画到一半发现放不下就得重来。"""
    sheet_w = CANVAS_WIDTH - M.pad_body_x * 2
    content_x = M.pad_body_x + M.sheet_pad_left
    content_w = sheet_w - M.sheet_pad_left - M.sheet_pad_right

    # 抬头右侧是"日期"与"星期"上下两行，合计才是它占的高度
    head_h = pen.height(M.size_date, True) + pen.height(M.size_weekday) + M.head_pad_bottom

    rows: list[dict[str, Any]] = []
    for entry in ctx.get("entries") or []:
        # 模板里 `p + p` 与 `.entry + .entry` 都留了一整行 --lh 的空行。
        # 空行**仍然画横格线**（CSS 那条 repeating-linear-gradient 是背景，不因为没有文字就断开），
        # 所以它是一行"只有线没有字"的行，不是把行距撑开。
        if rows:
            rows.append({"kind": "gap"})
        rows.append({"kind": "meta", "time": entry.get("time") or "",
                     "mood": entry.get("mood") or "", "who": entry.get("who") or ""})
        for para in entry.get("paragraphs") or []:
            if any(r["kind"] != "gap" and r["kind"] != "meta" for r in rows[-1:]):
                rows.append({"kind": "gap"})
            for line in pen.wrap(str(para), M.size_body, content_w):
                rows.append({"kind": "text", "line": line})

    body_h = len(rows) * M.lh
    foot_h = M.foot_margin_top + M.foot_pad_top + pen.height(M.size_foot) + 8
    sheet_h = M.sheet_pad_top + head_h + body_h + foot_h + M.sheet_pad_bottom
    return {
        "sheet_w": sheet_w, "sheet_x": M.pad_body_x, "sheet_h": sheet_h,
        "content_x": content_x, "content_w": content_w,
        "head_h": head_h, "rows": rows, "body_h": body_h, "foot_h": foot_h,
    }


def _draw(ctx: dict[str, Any], pen: _Pen, plan: dict[str, Any]) -> Any:
    from PIL import Image, ImageDraw

    s = pen.scale
    width = CANVAS_WIDTH * s
    height = plan["sheet_h"] * s
    image = Image.new("RGB", (width, height), M.color_page)
    draw = ImageDraw.Draw(image)

    sx, sy = plan["sheet_x"] * s, 0
    sw = plan["sheet_w"] * s
    draw.rectangle([sx, sy, sx + sw, height], fill=M.color_sheet)

    cx = plan["content_x"] * s
    cw = plan["content_w"] * s

    # 装订线 + 三个装订孔
    holes_w = M.sheet_pad_left * s
    draw.line([sx + holes_w, 0, sx + holes_w, height], fill=M.color_margin, width=max(1, s // 2))
    r = M.holes_d * s // 2
    hx = sx + M.holes_left * s + r
    for cy in (M.holes_top * s, height // 2, height - M.holes_bottom * s):
        draw.ellipse([hx - r, cy - r, hx + r, cy + r], fill=M.color_hole,
                     outline="#e6dccf", width=max(1, s // 2))

    # ---- 抬头
    y = M.sheet_pad_top * s
    pen.draw_text(draw, (cx, y + pen.px(6)), f"{ctx.get('book_name') or ''} · 共 "
                  f"{int(ctx.get('count') or 0)} 段", M.size_book, M.color_book,
                  track=M.track_book)
    date = str(ctx.get("date") or "")
    dw = pen.width(date, M.size_date, True) + int(M.track_date * M.size_date * s) * max(0, len(date) - 1)
    pen.draw_text(draw, (cx + cw - dw, y), date, M.size_date, M.color_date,
                  bold=True, track=M.track_date)
    pen.draw_text(draw, (cx + cw - pen.width(str(ctx.get("weekday") or ""), M.size_weekday),
                         y + pen.px(M.size_date + 4)),
                  str(ctx.get("weekday") or ""), M.size_weekday, M.color_weekday,
                  track=M.track_weekday)
    y += pen.px(plan["head_h"])
    draw.line([cx, y - max(1, s // 2), cx + cw, y - max(1, s // 2)], fill=M.color_line,
              width=max(1, s // 2))

    # ---- 正文（横格线画在文字底下）
    body_top = y
    for row in plan["rows"]:
        draw.line([cx, y - max(1, s // 2), cx + cw, y - max(1, s // 2)],
                  fill=M.color_rule, width=max(1, s))
        if row["kind"] == "text":
            pen.draw_text(draw, (cx, y + pen.px(6)), row["line"], M.size_body, M.color_ink)
        elif row["kind"] == "meta":
            _draw_meta(draw, pen, row, cx, y)
        y += pen.px(M.lh)
    y = body_top + plan["body_h"] * s
    if not plan["rows"]:  # 空正文也要有一条底线，否则页脚会贴着纸顶
        draw.line([cx, y - max(1, s // 2), cx + cw, y - max(1, s // 2)],
                  fill=M.color_rule, width=max(1, s))

    # ---- 页脚
    y += pen.px(M.foot_margin_top)
    draw.line([cx, y, cx + cw, y], fill=M.color_line, width=max(1, s // 2))
    y += pen.px(M.foot_pad_top)
    pen.draw_text(draw, (cx, y), "她写下的这一天", M.size_foot, M.color_foot,
                  track=M.track_foot)
    total = int(ctx.get("page_total") or 0)
    index = int(ctx.get("page_index") or 0)
    if total > 0 and 0 < index <= total:
        page = f"第 {index} / {total} 页"
        pen.draw_text(draw, (cx + cw - pen.width(page, M.size_foot), y), page,
                      M.size_foot, M.color_foot, track=M.track_foot)
    return image


def _draw_meta(draw: Any, pen: _Pen, row: dict[str, Any], x: int, y: int) -> None:
    """一行 ``时间 + 心情胶囊 + 和谁``，基线对齐（照 CSS 的 ``align-items: baseline``）。"""
    s = pen.scale
    baseline = y + pen.px(M.lh * 0.5)
    pen.draw_text(draw, (x, baseline - pen.height(M.size_time, True)), row["time"],
                  M.size_time, M.color_time, bold=True)

    cursor = x + pen.width(row["time"], M.size_time, True) + pen.px(M.meta_gap)
    mood = row.get("mood") or ""
    if mood:
        mw = pen.width(mood, M.size_mood)
        pad = pen.px(M.mood_pad_x)
        top = baseline - pen.px(M.lh * 0.5) + pen.px(8)
        bottom = top + pen.height(M.size_mood) + pen.px(4)
        draw.rounded_rectangle([cursor, top, cursor + mw + pad * 2, bottom],
                               radius=(bottom - top) // 2, fill=M.color_mood_bg,
                               outline=M.color_mood_border, width=max(1, s // 2))
        pen.draw_text(draw, (cursor + pad, top + pen.px(1)), mood, M.size_mood, M.color_mood)
        cursor += mw + pad * 2 + pen.px(M.meta_gap)

    who = row.get("who") or ""
    if who:
        pen.draw_text(draw, (cursor, baseline - pen.height(M.size_who)), who,
                      M.size_who, M.color_who)


def render_sync(ctx: dict[str, Any], dest: Path, *, scale: int = SCALE) -> Path:
    """同步画一张卡并写到 ``dest``。**CPU 活，别在事件循环里直接调**（用 :func:`render`）。"""
    pen = _Pen(scale)
    plan = _layout(ctx, pen)
    image = _draw(ctx, pen, plan)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, format="PNG", optimize=True)
    return dest


async def render(ctx: dict[str, Any], dest: Path, *, scale: int = SCALE) -> Path:
    """把模板数据画成 PNG。失败一律抛 :class:`PilRenderError`（调用方只认这一种）。"""
    ok, reason = probe()
    if not ok:
        raise PilRenderError(reason)
    try:
        return await asyncio.to_thread(functools.partial(render_sync, ctx, dest, scale=scale))
    except PilRenderError:
        raise
    except Exception as error:  # noqa: BLE001 - 统一收敛，编排层只捕这一种
        raise PilRenderError(f"PIL 出图失败：{error}") from error


async def shutdown() -> None:
    """没有要收的：PIL 不留进程、不留连接。

    留着是为了和 ``card_local.shutdown()`` 对称——三个引擎的收尾写成一个形状，
    编排层就可以无差别地挨个调，将来真有了要收的东西也不用改调用点。
    """
    with contextlib.suppress(RuntimeError):
        await asyncio.sleep(0)


__all__ = ["CANVAS_WIDTH", "FONT_ENV", "M", "SCALE", "PilRenderError",
           "find_bold_font", "find_font", "probe", "render", "render_sync", "shutdown"]