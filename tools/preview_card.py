"""日记卡本地预览（第 15 步配套）：把 ``template/*.html.j2`` 渲染成能直接打开的 HTML。

    py tools/preview_card.py                 # 三款并排写到 test/.tmp/cards/
    py tools/preview_card.py paper           # 只看某一款
    py tools/preview_card.py --open paper    # 顺手调起默认浏览器

**为什么需要它。** 卡片真正的出图链路是 ``html_render`` → 远端无头浏览器
（``astrbot/core/utils/t2i``），那条路要 AstrBot 起着、要出网、还只看得到最终那张
PNG——改一行 CSS 看不到中间态，"好不好看"只能靠猜。本工具把模板在本地渲成 HTML，
用浏览器直接看，用来迭代排版。

**为什么自己实现模板渲染。** 仓库零第三方依赖是硬约束（``core/`` 那条铁律），
而这里的模板只用到 Jinja2 的 ``{{ }}`` / ``{% for %}`` / ``{% if %}`` 三种语法，
不值得为预览引入 jinja2。**这不是通用 Jinja2**，只认下面这几种写法；模板用别的
语法会渲不出来（渲不出来会直接报错，不会静默出空图）。

⚠️ 这里**不**做 HTML 转义——转义在 ``main.py`` 落进 data 之前就做完了（那条渲染链
没有 autoescape）。预览数据是假的，模板里也不写 ``|safe``。
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PLUGIN_DIR / "template"
OUT_DIR = PLUGIN_DIR / "test" / ".tmp" / "cards"

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 假数据：覆盖"一段多段""有心情无心情""有标注无标注""长段落"几种形态。
SAMPLE = {
    "date": "2026-10-04",
    "weekday": "周日",
    "book_name": "日记",
    "count": 2,
    "entries": [
        {
            "time": "09:12",
            "mood": "安心",
            "who": "希",
            "paragraphs": [
                "今天把窗户推开了一条缝，外面的风终于不冷了。",
                "她记下了这句，没写为什么。",
            ],
        },
        {
            "time": "21:13",
            "mood": "",
            "who": "",
            "paragraphs": [
                "晚上读到一半就困了，书还摊在桌上。窗外的车灯一盏一盏地过去，"
                "忽然觉得这样也挺好的——不用做什么，光是看着就已经很够了。"
                "睡之前记得把台灯关掉。",
            ],
        },
    ],
}

_TAG = re.compile(r"{%-?\s*(.*?)\s*-?%}", re.S)
_VAR = re.compile(r"{{\s*(.*?)\s*}}", re.S)


def _walk(expr: str, ctx: dict):
    """``a.b.0.c`` → 沿点取值，取不到返回 ``None``（**保留原对象**，不转字符串）。"""
    node: object = ctx
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


def _lookup(expr: str, ctx: dict) -> str:
    """给 ``{{ }}`` 用的字符串取值；取不到返回空串（模板不该因一条空字段炸掉）。"""
    node = _walk(expr, ctx)
    if node is None or node is False:
        return ""
    if isinstance(node, (list, tuple)):
        return ""
    return str(node)


def _truthy(expr: str, ctx: dict) -> bool:
    node = _walk(expr, ctx)
    if isinstance(node, str):
        return node not in ("", "0", "False")
    return bool(node)


def render(template: str, ctx: dict) -> str:
    """渲染模板：只支持 ``{{ x }}`` / ``{% for x in y %}`` / ``{% if x %}…{% endif %}``。"""
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
            out.append(_lookup(_VAR.match(template[first : end + 2]).group(1), ctx))
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
            out.append("")
    return "".join(out)


def _consume_block(template: str, pos: int) -> tuple[str, int]:
    """从 ``pos`` 取到配对的 endif / endfor，返回 ``(块内容, 结束后的位置)``。

    块尾按**标签实际起点**切，不用 ``len(body)`` 反推——``{% endfor %}`` 与
    ``{%- endfor -%}`` 长度不同，靠算术必然切错（第一版就栽在这，for 循环整段没渲出来）。
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


def build(style: str, ctx: dict) -> str:
    src = (TEMPLATE_DIR / f"diary_card_{style}.html.j2").read_text(encoding="utf-8")
    return render(src, ctx)


def default_styles() -> list[str]:
    """默认渲染哪几款——**读枚举**，别在这里另写一份。

    写死过一次：加了 grid/night/kraft 忘了改这里，``py tools/preview_card.py``
    照样只渲三款，还让人以为新模板有问题。
    """
    sys.path.insert(0, str(PLUGIN_DIR))
    try:
        from core.settings import CARD_STYLE_CHOICES  # noqa: PLC0415

        return list(CARD_STYLE_CHOICES)
    except Exception:  # noqa: BLE001 - 预览工具，不该因导入失败而不能用
        return ["paper", "ink", "postcard"]


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    do_open = "--open" in argv
    styles = args or default_styles()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for style in styles:
        try:
            page = build(style, SAMPLE)
        except FileNotFoundError:
            print(f"× 没有这个款式：{style}")
            continue
        target = OUT_DIR / f"diary_card_{style}.html"
        target.write_text(page, encoding="utf-8")
        written.append(target)
        print(f"✓ {target}")

    index = OUT_DIR / "index.html"
    cols = len(written)
    index.write_text(
        "<!DOCTYPE html><meta charset='utf-8'>"
        "<title>日记卡预览</title>"
        "<style>body{margin:0;padding:24px;background:#5a5f6b;font:14px/1.6 "
        "'Microsoft YaHei',sans-serif;display:grid;gap:24px;"
        f"grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}}"
        "iframe{border:0;width:100%;height:1900px;background:#fff}</style>"
        + "".join(
            f"<div><div style='color:#fff;padding:0 0 8px'>{w.stem}</div>"
            f"<iframe src='{w.name}' scrolling='no'></iframe></div>"
            for w in written
        ),
        encoding="utf-8",
    )
    print(f"✓ {index}（{cols} 款并排）")

    if do_open and written:
        import os

        os.startfile(index)  # noqa: S606 - Windows 打开默认浏览器
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))