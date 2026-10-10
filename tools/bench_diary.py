"""日记读路径的**实测**规模基准（第 17 步起；第 18 步补 JSONL 权威路径）。

为什么先测再决定：上数据库是很大的改动，凭感觉拍"以后会慢"或者"现在没事"都是不负责。
这个脚本拿真代码跑真数据，把每条读路径的耗时量出来——**按人过滤、日期查找、全文重
解析**各是多少，旧 txt 导入解析与 JSONL 权威解析各测一份。

    py tools/bench_diary.py            # 默认 1 年 / 3 年 / 10 年
    py tools/bench_diary.py 365 1095 3650

结论看输出里"全量解析"那一列：那是几乎**每条**读路径都会付的固定成本。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.diary import format as fmt  # noqa: E402

BODY = (
    "今天把窗户推开了一条缝，外面的风终于不冷了。\n"
    "傍晚去楼下走了两圈，买了点水果，回来路上天已经黑透了。"
)


def build_entries(n: int) -> list[fmt.Entry]:
    start = datetime(2020, 1, 1)
    return [
        fmt.Entry(
            date=(start + timedelta(days=i)).strftime("%Y-%m-%d"),
            time="09:12",
            mood="安心",
            who=f"和示例{10000 + i}",
            person_id=str(10000 + i),
            text=BODY,
        )
        for i in range(n)
    ]


def timed(fn, repeat: int = 5):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def bench(n: int) -> None:
    entries = build_entries(n)
    legacy_text = fmt.view_text(entries)          # 旧 .txt 版式（导入路径的输入）
    jsonl_text = "".join(fmt.entry_line(e) + "\n" for e in entries)
    size_mb = len(jsonl_text.encode("utf-8")) / 1024 / 1024

    tz = timezone.utc
    now = datetime(2029, 1, 1, tzinfo=tz)
    segments = fmt.split_segments(legacy_text)

    t_jsonl = timed(lambda: fmt.entries_from_jsonl(jsonl_text))
    t_legacy = timed(lambda: fmt.parse_entries(legacy_text))
    t_find = timed(lambda: fmt.find_segment(segments, BODY[:8], now, 7, tz))
    t_day = timed(lambda: [e for e in entries if e.date == "2025-06-15"], repeat=50)
    t_by_person = timed(
        lambda: [e for e in entries if e.person_id == "10001"], repeat=20
    )
    t_segid = timed(
        lambda: [fmt.entry_seg_id(e) for e in entries[:200]], repeat=20
    )

    print(f"\n{n:>6} 段 · {size_mb:6.2f} MB（JSONL）")
    print(f"   全量解析 entries_from_jsonl {t_jsonl:8.2f} ms   ← 权威读路径，几乎每条读都要付")
    print(f"   旧 txt 导入 parse_entries   {t_legacy:8.2f} ms   ← 只在升级导入那一次")
    print(f"   按片段找 find_segment       {t_find:8.2f} ms   ← 改 / 删一次")
    print(f"   按日期找（已解析）          {t_day:8.4f} ms")
    print(f"   按人筛（已解析）            {t_by_person:8.4f} ms")
    print(f"   内容地址 entry_seg_id ×200  {t_segid:8.2f} ms   ← 面板改删一次")


def main(argv: list[str]) -> int:
    sizes = [int(a) for a in argv[1:] if a.isdigit()] or [365, 1095, 3650]
    print("日记读路径规模基准（每项取 5 次最好值）")
    for n in sizes:
        bench(n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
