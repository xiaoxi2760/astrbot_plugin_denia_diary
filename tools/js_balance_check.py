"""前端 JS 的**离线语法粗检**（Node 缺席时的代偿）：剥掉注释与字符串后核对
括号是否配平。不是真正的语法解析——只是把"少一个花括号"这类低级错误拦在交付前。
Node 可用的环境请跑 ``node --test test/frontend/``（那是权威前端测试）。
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
FILES = [
    "pages/diary/app.js",
    "pages/diary/ui.js",
    "pages/diary/views/settings.js",
    "pages/diary/views/proactive.js",
]


def strip(src: str) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if ch == "/" and nxt == "/":
            end = src.find("\n", i)
            i = n if end < 0 else end
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            out.append(quote)
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    break
                out.append("x" if src[i] not in "\n\r" else src[i])
                i += 1
            out.append(quote)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def main() -> int:
    failures: list[str] = []
    for rel in FILES:
        src = (PLUGIN / rel).read_text(encoding="utf-8")
        clean = strip(src)
        for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
            if clean.count(op) != clean.count(cl):
                failures.append(f"{rel}: {op}{cl} 不配平（{clean.count(op)} vs {clean.count(cl)}）")
        if clean.count('"') % 2 or clean.count("'") % 2:
            failures.append(f"{rel}: 引号数可疑（字符串剥离可能有洞）")
    for line in failures:
        print(f"✗ {line}")
    if failures:
        return 1
    print("✓ 前端 JS 括号/引号粗检全过：" + "、".join(FILES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
