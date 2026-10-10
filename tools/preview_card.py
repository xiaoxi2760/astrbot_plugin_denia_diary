"""日记卡本地预览：把 ``template/*.html.j2`` 渲成能直接打开的 HTML。

    py tools/preview_card.py             # 全部款式写到 test/.tmp/cards/
    py tools/preview_card.py paper       # 只看某一款
    py tools/preview_card.py --open paper

**为什么需要它。** 卡片真正的出图链路是 ``html_render`` → 远端无头浏览器
（``astrbot/core/utils/t2i``），那条路要 AstrBot 起着、要出网、还只看得到最终那张
PNG——改一行 CSS 看不到中间态，"好不好看"只能靠猜。

**渲染逻辑不在这里**——和 ``/看日记`` 出图、WebUI 卡片预览共用
``core.diary/card.py`` 的那一份（见那里的说明：本地没有 Jinja2，模板又只用到
三种语法）。这里只管造假数据 + 写文件 + 开浏览器。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.diary import card  # noqa: E402
from core.settings import CARD_STYLE_CHOICES  # noqa: E402

OUT_DIR = PLUGIN_DIR / "test" / ".tmp" / "cards"

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


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    do_open = "--open" in argv
    styles = args or list(CARD_STYLE_CHOICES)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for style in styles:
        try:
            page = card.render_card(style, SAMPLE)
        except (OSError, card.TemplateError) as error:
            print(f"× {style}：{error}")
            continue
        target = OUT_DIR / f"diary_card_{style}.html"
        target.write_text(page, encoding="utf-8")
        written.append(target)
        print(f"✓ {target}")

    index = OUT_DIR / "index.html"
    index.write_text(
        "<!DOCTYPE html><meta charset='utf-8'>"
        "<title>日记卡预览</title>"
        "<style>body{margin:0;padding:24px;background:#5a5f6b;font:14px/1.6 "
        "'Microsoft YaHei',sans-serif;display:grid;gap:24px;"
        "grid-template-columns:repeat(auto-fit,minmax(460px,1fr))}"
        "iframe{border:0;width:100%;height:1900px;background:#fff}</style>"
        + "".join(
            f"<div><div style='color:#fff;padding:0 0 8px'>{w.stem}</div>"
            f"<iframe src='{w.name}' scrolling='no'></iframe></div>"
            for w in written
        ),
        encoding="utf-8",
    )
    print(f"✓ {index}（{len(written)} 款并排）")

    if do_open and written:
        import os

        os.startfile(index)  # noqa: S606 - Windows 打开默认浏览器
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))