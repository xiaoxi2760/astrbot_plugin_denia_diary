"""日记卡的**服务端渲染**（第 16 步）：/看日记 出图、WebUI 卡片预览共用这一份。

## 为什么要在插件里自己渲染

``/看日记`` 的出图链路是 ``html_render`` → **远端** t2i 服务，那边才有 Jinja2；
宿主本地（AstrBot 的 ``requirements.txt``）**没有** jinja2，装了也没用。

而面板里要预览卡片——iframe 里的前端**没有** Jinja2，也不该自己解析模板（模板是
产品文件，能改的是插件不是页面）。所以渲染必须发生在插件里：``/看日记`` 先在这里
渲成 HTML 再 POST 出去，面板预览则直接把这段 HTML 交给 iframe。
**两条路共用 ``render_card``**，预览看到的就是真卡片，不是"大概长这样"。

## 为什么自己实现这几十行而不是引 jinja2

仓库零第三方依赖是硬约束（见 DEVELOPMENT.md）。模板只用到 Jinja2 的
``{{ }}`` / ``{% for %}`` / ``{% if %}`` 三种语法，为这点需求引入一整个模板引擎
不值得。

⚠️ **代价**：这不是通用 Jinja2，只认下面三种写法。模板用了别的语法（比如
``{% set %}``、过滤器、宏）会**抛异常**而不是静默出空图——宁可炸也不要给一张
空白的"预览"。``test_diary.py`` 有一项把六款模板逐个真渲一遍的测试盯着这条。
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent
TEMPLATE_DIR = PLUGIN_DIR / "template"

_TAG = re.compile(r"{%-?\s*(.*?)\s*-?%}", re.S)
_VAR = re.compile(r"{{\s*(.*?)\s*}}", re.S)

WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
"""给卡片抬头用。与 ``main.py`` 的 ``_card_weekday`` 同源（那边给 Markdown 用）。"""

STYLE_LABELS: dict[str, str] = {
    "paper": "纸感",
    "ink": "墨信",
    "postcard": "明信片",
    "tape": "手帐",
    "dots": "点阵本",
    "seal": "火漆信笺",
}
"""款式键 → 中文名。**单一来源**：``/日记款式`` 指令、面板版式按钮、设置页下拉框
都用这张表，面板上再不会出现 ``paper`` / ``tape`` 这种要人猜的英文键。"""

RENDER_OPTIONS: dict[str, Any] = {"type": "png", "quality": 95}
"""出图参数（会覆盖宿主 ``html_render`` 的默认值）。

⚠️ 宿主的默认是 ``{"type": "jpeg", "quality": 40}``（见 AstrBot
``astrbot/core/utils/t2i/network_strategy.py``）。40 的 JPEG 压 15~16px 的中文小字，
字缘全是振铃、纸纹和横格线直接断层——卡片"丑"有一大半是它造成的，不是模板。
这里改成 PNG + q95：字干净了，代价是单张从几百 KB 涨到 1~3 MB（发 QQ 有流量代价，
但对一天发一次的卡片可以接受）。未知键会被远端忽略，多传无副作用。

原本写在 ``main.py`` 里，但面板的"导出为图片"也要用同一份参数——两张图必须长得
一模一样，所以常量挪到这儿，两边都从这里取。"""

_FONT_FAMILY_RE = re.compile(r"^[A-Za-z0-9 一-鿿,._'\-()]{1,180}$")
"""字体名白名单：只放行"字体名/字体栈"该有的字符。"""
_FONT_URL_RE = re.compile(
    r"^https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+\.(?:woff2?|ttf|otf|eot)$",
    re.IGNORECASE,
)
"""字体文件直链：只放行 http(s) + 常见字体后缀，杜绝 ``javascript:`` 之类的伪协议。"""

_FONT_FAMILY_NAME = "CardFont"
"""注入的 @font-face 家族名。模板里正文统一用它，配置为空时它压根不会被定义——
``font-family`` 拿不到这个家族就自动回落款式自带的字体栈。"""


def font_css(raw: object) -> str:
    """把配置里的 ``diary.card_font`` 变成一段可安全内联的 CSS（空串＝不改字体）。

    两种写法都认：字体文件直链 → ``@font-face``；字体名 / 字体栈 → 直接当
    ``font-family``。

    ⚠️ 这一段是**原样**进模板 ``<style>`` 的（不像正文那样 html.escape——它是 CSS，
    转义了就废了），所以必须自己把住关：两个正则都是白名单式匹配，任何一个不过就
    整条丢弃、退回模板默认字体。**宁可字体没生效，也不能让人往配置里塞 ``</style>``。**
    """
    value = str(raw or "").strip()
    if not value:
        return ""
    if _FONT_URL_RE.match(value):
        return (
            f"@font-face{{font-family:'{_FONT_FAMILY_NAME}';"
            f"src:url('{value}') format('woff2');font-display:swap;}}"
            f"body{{font-family:'{_FONT_FAMILY_NAME}',var(--card-font-stack);}}"
        )
    if _FONT_FAMILY_RE.match(value):
        return f"body{{font-family:{value},var(--card-font-stack);}}"
    return ""


class TemplateError(ValueError):
    """模板用到了本渲染器**不支持**的语法（而不是"数据缺了个键"）。"""


# ---- 取值 ---------------------------------------------------------------------


def _walk(expr: str, ctx: Any):
    """``a.b.0.c`` → 沿点取值，取不到返回 ``None``（**保留原对象**，不转字符串）。"""
    node: Any = ctx
    for part in expr.strip().split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, (list, tuple)):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if node is None:
            return None
    return node


def _lookup(expr: str, ctx: Any) -> str:
    """给 ``{{ }}`` 用的字符串取值；取不到返回空串。"""
    node = _walk(expr, ctx)
    if node is None or node is False:
        return ""
    if isinstance(node, (list, tuple)):
        return ""
    return str(node)


def _truthy(expr: str, ctx: Any) -> bool:
    node = _walk(expr, ctx)
    if isinstance(node, str):
        return node not in ("", "0", "False")
    return bool(node)


# ---- 块定位 -------------------------------------------------------------------


def _consume_block(template: str, pos: int) -> tuple[str, int]:
    """从 ``pos`` 取到配对的 endif / endfor，返回 ``(块内容, 结束后的位置)``。

    块尾按**标签实际起点**切，不用 ``len(body)`` 反推——``{% endfor %}`` 与
    ``{%- endfor -%}`` 长度不同，靠算术必然切错。
    """
    depth = 1
    start = pos
    while pos < len(template):
        tag_at = template.find("{%", pos)
        var_at = template.find("{{", pos)
        candidates = [x for x in (tag_at, var_at) if x != -1]
        if not candidates:
            return template[start:], len(template)
        first = min(candidates)
        end_t = template.find("%}", first)
        end_v = template.find("}}", first)
        if first == var_at and (end_t == -1 or (end_v != -1 and end_v < end_t)):
            pos = (end_v + 2) if end_v != -1 else len(template)
            continue
        if end_t == -1:
            return template[start:], len(template)
        body = _TAG.match(template[first : end_t + 2]).group(1).strip()
        pos = end_t + 2
        if body.startswith(("for ", "if ")):
            depth += 1
        elif body in ("endif", "endfor"):
            depth -= 1
            if depth == 0:
                return template[start:first], pos
    return template[start:], len(template)


# ---- 渲染 ---------------------------------------------------------------------


def render(template: str, ctx: dict[str, Any]) -> str:
    """渲染模板。支持 ``{{ x }}`` / ``{% for x in y %}`` / ``{% if x %}…{% endif %}``。"""
    out: list[str] = []
    pos = 0
    while pos < len(template):
        nxt_tag = template.find("{%", pos)
        nxt_var = template.find("{{", pos)
        if nxt_tag == -1 and nxt_var == -1:
            out.append(template[pos:])
            break
        first = min(x for x in (nxt_tag, nxt_var) if x != -1)
        out.append(template[pos:first])

        if first == nxt_var:
            end = template.find("}}", first)
            if end == -1:
                out.append(template[first:])
                break
            expr = _VAR.match(template[first : end + 2]).group(1)
            if "|" in expr:
                # Jinja2 的过滤器。本渲染器不支持它——静默返回空串的话，模板作者会
                # 看到一张"少了几个字"的图，压根不知道是过滤器没生效。
                raise TemplateError(f"不支持的表达式（像是过滤器）：{{{{ {expr} }}}}")
            out.append(_lookup(expr, ctx))
            pos = end + 2
            continue

        end = template.find("%}", first)
        if end == -1:
            out.append(template[first:])
            break
        body = _TAG.match(template[first : end + 2]).group(1).strip()
        pos = end + 2

        if body.startswith("for "):
            # 剥 "for "，再按 " in " 切成 变量名 / 迭代式。
            # ⚠️ 别用 tail.partition(" ")：那样 iterable 会拿到 "in entries"。
            name, _, iterable = body[4:].strip().partition(" in ")
            inner, pos = _consume_block(template, pos)
            seq = _walk(iterable.strip(), ctx)
            for item in (seq if isinstance(seq, (list, tuple)) else []):
                child = dict(ctx)
                child[name.strip()] = item
                out.append(render(inner, child))
        elif body.startswith("if "):
            cond = body[3:].strip()
            inner, pos = _consume_block(template, pos)
            if _truthy(cond, ctx):
                out.append(render(inner, ctx))
        else:
            raise TemplateError(f"模板用到了不支持的语法：{{% {body} %}}")

    return "".join(out)


# ---- 卡片 ---------------------------------------------------------------------


def card_context(
    *,
    date: str,
    weekday: str,
    book_name: str,
    entries: list[dict[str, Any]],
    font_css: str = "",
    page_index: int = 0,
    page_total: int = 0,
) -> dict[str, Any]:
    """条目 → 模板数据。**HTML 转义只在这一处做**。

    出图那条链路没有 autoescape（远端服务默认不开），所以 ``< > &`` 必须在这里
    就变成实体——否则她写的 ``<b>`` 会在卡片里变成真标签。
    面板预览走同一份，所以看到的就是真效果。

    ``page_index`` / ``page_total`` 是**这一页在整本日记里的位置**（1 起），
    印在卡片右下角 —— 日记本总有页码，缺了就像一叠散纸。两个都给 0（或任一为 0）
    就不印，模板里 ``{% if page_total %}`` 自己会跳过。
    """
    total = int(page_total or 0)
    index = int(page_index or 0)
    # 越界或没给就当"没有页码"：宁可右下角空着，也不要印一个「第 9 / 3 页」
    if total <= 0 or index <= 0 or index > total:
        index, total = 0, 0
    return {
        "date": str(date or ""),
        "weekday": str(weekday or ""),
        "book_name": str(book_name or ""),
        "count": len(entries),
        "page_index": index,
        "page_total": total,
        "card_font_css": str(font_css or ""),
        "entries": [
            {
                "time": html.escape(str(item.get("time") or ""), quote=False),
                "mood": html.escape(str(item.get("mood") or ""), quote=False),
                "who": html.escape(str(item.get("who") or ""), quote=False),
                "paragraphs": [
                    html.escape(str(para), quote=False)
                    for para in (item.get("paragraphs") or [])
                ],
            }
            for item in entries
        ],
    }


def template_path(style: str) -> Path:
    return TEMPLATE_DIR / f"diary_card_{style}.html.j2"


def render_card(style: str, ctx: dict[str, Any]) -> str:
    """按款式渲一张卡片的 HTML。模板读不到 → ``OSError``（调用方走降级）。"""
    path = template_path(style)
    if not path.is_file():
        raise TemplateError(f"没有这一款模板：{path.name}")
    return render(path.read_text(encoding="utf-8"), ctx)


__all__ = [
    "TEMPLATE_DIR",
    "WEEKDAYS",
    "TemplateError",
    "card_context",
    "font_css",
    "render",
    "render_card",
    "template_path",
]