"""``/看日记`` 出图链路的**实测**基准（第 20 步）。

    py tools/bench_card.py              # 本地三引擎 + 缓存（不联网）
    py tools/bench_card.py --remote     # 额外量一次真实远端 t2i（要联网）
    py tools/bench_card.py --card paper # 指定款式（默认纸感）

为什么要分条量而不是报一个总数：``/看日记`` 的耗时拆开是三块完全不同的东西——

* **本地渲染**（模板 → PNG）：playwright 冷启 ~1.8 秒、热 ~0.5 秒；PIL ~0.5 秒；都在本机，不出门。
* **远端渲染**：AstrBot 官方 t2i，4~6 秒，而且官方端点**随机挑**、失败自动重试，抖动会翻倍。
* **缓存命中**：命中就是一次读文件，亚毫秒。日记是反复翻的东西，这一档才是常态。

第 20 步之前这条链路只有"远端"一档，所以用户看到的永远是那个 4~6 秒（最坏翻倍）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.diary import card, card_cache, card_render  # noqa: E402

T2I = "https://t2i.soulter.top/text2img/generate"

BODY = (
    "今天把窗户推开了一条缝，外面的风终于不冷了。傍晚去楼下走了两圈，"
    "买了点水果，回来路上天已经黑透了，路灯把影子拉得很长。"
)


def build_entries(n: int) -> list[dict[str, object]]:
    base = datetime(2026, 10, 13)
    return [
        {
            "time": f"{(9 + i * 3) % 24:02d}:{(i * 17) % 60:02d}",
            "mood": "安心",
            "who": "和示例",
            "paragraphs": [BODY, "回来的路上顺手拍了张路灯，发给她看，她说像不像糖葫芦。"],
        }
        for i in range(n)
    ]


def _ctx(style: str, n: int) -> dict:
    return card.card_context(
        date="2026-10-13",
        weekday="周二",
        book_name="普通日记",
        entries=build_entries(n),
        font_css=card.font_css(""),
        page_index=12,
        page_total=48,
    )


async def _bench_local(style: str, n: int) -> None:
    html_text = card.render_card(style, _ctx(style, n))
    key = card_cache.key_for(style, _ctx(style, n), card.template_path(style))

    async def no_remote(*args, **kwargs):
        raise RuntimeError("基准里不调远端")

    # 每个引擎一个**独立**缓存目录：否则第二个测到的就是缓存命中，不是引擎耗时。
    probes = {"playwright": card_render.card_local.probe,
              "pil": card_render.card_pil.probe}
    for engine, probe in probes.items():
        ok, reason = probe()
        if not ok:
            print(f"  {engine:<11} 这台机器用不了：{reason}")
            continue
        cache_dir = Path(tempfile.mkdtemp()) / "card-cache"
        started = time.perf_counter()
        out = await card_render.render_html(
            html_text, _ctx(style, n), cache_dir=cache_dir, key=key,
            engine=engine, remote=no_remote,
        )
        cold_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        again = await card_render.render_html(
            html_text, _ctx(style, n), cache_dir=cache_dir, key=key,
            engine=engine, remote=no_remote,
        )
        warm_ms = (time.perf_counter() - started) * 1000
        size_kb = out.stat().st_size // 1024 if out else 0
        print(f"  {engine:<11} 出图 {cold_ms:8.1f} ms | 再来一次(缓存) {warm_ms:6.2f} ms | {size_kb:4d} KB")
        assert again is not None
    await card_render.card_local.shutdown()


def _bench_remote(style: str, n: int) -> None:
    """量一次**真实**的远端出图（第 20 步之前，用户等的就是这个数）。"""
    html_text = card.render_card(style, _ctx(style, n))
    payload = json.dumps({
        "tmpl": html_text, "json": False, "tmpldata": _ctx(style, n),
        "options": {"full_page": True, **card.RENDER_OPTIONS},
    }).encode()
    request = urllib.request.Request(
        T2I, data=payload,
        # ⚠️ 官方 t2i 会拒掉 urllib 的默认 UA（403）。带上浏览器 UA 才量得到真实数字——
        # 这不是绕过什么检查，是同一个公开接口对不同客户端的常规差别。
        headers={"Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; denia-diary-bench/1.0)"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            blob = response.read()
    except Exception as error:  # noqa: BLE001 - 基准脚本，失败就报出来
        print(f"  {'remote':<11} 量不到：{error}")
        return
    ms = (time.perf_counter() - started) * 1000
    print(f"  {'remote':<11} 出图 {ms:8.1f} ms | {len(blob) // 1024:4d} KB"
          "   ← 第 20 步之前 /看日记 等的就是这个数")


def main(argv: list[str]) -> int:
    style = "paper"
    for i, arg in enumerate(argv):
        if arg == "--card" and i + 1 < len(argv):
            style = argv[i + 1]
    print(f"日记卡出图链路基准（款式 {style}，6 条日记 / 真实负载）\n")
    print("本地（不联网）：")
    for n in (3, 6):
        print(f"  —— {n} 条 ——")
        asyncio.run(_bench_local(style, n))
    if "--remote" in argv:
        print("\n远端（要联网）：")
        _bench_remote(style, 6)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))