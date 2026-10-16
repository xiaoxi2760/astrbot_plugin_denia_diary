"""真端到端冒烟（第 17 步）：从**旧纯文本本子**一路跑到**渲染好的卡片 HTML**。

单元测试都是拿新数据搭的架子，碰不到两件真事：

1. **旧 ``日记.txt`` 导入 JSONL** —— 存量用户的本子长什么样，导入后还在不在；
2. **按人隔离** —— A 和 B 同一天各写各的，A 的卡片里绝不能混进 B 的。

所以这个脚本**用真插件代码**（复用 ``test_main`` 的 astrbot 桩加载 ``main.py``），
真造一个数据目录、真跑会话、真调 ``/看日记``、真拿面板接口，最后把卡片 HTML 落到
``test/.tmp/e2e/`` 供人眼过一遍。

    py tools/e2e_diary.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR / "test"))

from test_main import _load_plugin  # noqa: E402  复用 astrbot 桩，别再搭一套

OUT_DIR = PLUGIN_DIR / "test" / ".tmp" / "e2e"
TZ8 = "+08:00"

# 旧格式本子：① 头行带 #id（1.3 那版写的）② 头行不带 id（1.2 及更早）
# ③ 群聊条目（群·前缀，无归属）④ 没写完的半截
LEGACY_TXT = (
    "2026-10-01 09:00（开心）〔和希#1411638634〕\n"
    "九月的一个晴天。\n\n"
    "2026-10-04 08:30（期待）〔和希#1411638634〕\n"
    "早上的风是凉的。\n\n"
    "2026-10-04 21:30（难过）〔和小满#u_2002〕\n"
    "今天有点闷。\n\n"
    "2026-10-04 22:10\n群里的那句：算了。\n\n"
)


def make_event(umo: str, *, user_id: str, name: str, group: bool = False):
    kind = "GroupMessage" if group else "FriendMessage"
    from test_main import _FakeEvent

    kwargs = {"session_id": umo.split(":")[-1], "sender_id": user_id, "sender_name": name}
    if group:
        kwargs["group_id"] = "778899"
        kwargs["group_name"] = "测试群"

    class _Event(_FakeEvent):
        """给桩补上出结果要用的两个方法（测试里那些用例自带，这里没有）。"""

        def plain_result(self, text):
            return ("plain", text)

        def image_result(self, path):
            return ("image", path)

    return _Event(f"aiocqhttp:{kind}:{umo.split(':')[-1]}", **kwargs)


class Event:
    """收结果的假事件（只要能接住 ``plain_result`` / ``image_result``）。"""

    def __init__(self) -> None:
        self.plains: list[str] = []
        self.images: list[str] = []

    def plain_result(self, text):
        self.plains.append(str(text))
        return ("plain", text)

    def image_result(self, path):
        self.images.append(str(path))
        return ("image", path)


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    data_dir = OUT_DIR / "data"
    # ⚠️ **必须从干净目录起跑**：上一轮的日记.jsonl 会留在原地，旧 .txt 就会被
    # 再导入一遍叠加上去——第一版就因为这个把"4 段"看成"6 段"，查了半天才发现
    # 不是代码错、是脚手架脏。
    if data_dir.exists():
        shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # 先放旧本子，再让插件自己去导入
    (data_dir / "日记.txt").write_text(LEGACY_TXT, encoding="utf-8")
    print(f"旧本子已就位：{data_dir / '日记.txt'}（{len(LEGACY_TXT)} 字节，4 段）\n")

    module, _registry = _load_plugin(data_dir)
    module.StarTools = sys.modules["astrbot.api.star"].StarTools
    sys.modules["astrbot.api.star"].StarTools.data_dir = data_dir
    plugin = module.DeniaDiary(
        context=None,
        config={"timezone": "Asia/Shanghai", "name_preference": {"1411638634": "希", "u_2002": "小满"}},
    )
    diary = plugin.diary

    fails: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  {'✓' if ok else '✗'} {label}{('  —— ' + detail) if detail else ''}")
        if not ok:
            fails.append(label)

    # ---- 1. 旧 .txt 导入 ----
    print("① 旧纯文本本子导入 JSONL")
    entries = diary.store.read_entries(fmt_normal())
    check("导入后仍是 4 段", len(entries) == 4, f"实得 {len(entries)} 段")
    jsonl = data_dir / "日记.jsonl"
    check("已生成 JSONL", jsonl.is_file(), str(jsonl.name))
    if jsonl.is_file():
        lines = [l for l in jsonl.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("JSONL 一行一段", len(lines) == len(entries), f"{len(lines)} 行 / {len(entries)} 段")
        check("JSONL 每行都是合法 JSON", all(_json_ok(l) for l in lines))
    dated = sorted({e.date for e in entries})
    check("日期没丢", dated == ["2026-10-01", "2026-10-04"], str(dated))
    ids = sorted({e.person_id for e in entries})
    check(
        "带 #id 的两段回填到了归属",
        "1411638634" in ids and "u_2002" in ids,
        str(ids),
    )

    # ---- 2. 新写入走 JSONL ----
    print("\n② 新写入（新会话 → JSONL）")
    ev_a = make_event("1411638634", user_id="1411638634", name="希")
    ev_b = make_event("u_2002", user_id="u_2002", name="小满")
    res_a = await plugin.diary_write(ev_a, text="刚写的一条，只给希看。", mood="平静")
    res_b = await plugin.diary_write(ev_b, text="小满这边的一条。", mood="开心")
    check("A 写入成功", res_a.startswith("记下了") or "记" in res_a, res_a[:28])
    check("B 写入成功", res_b.startswith("记下了") or "记" in res_b, res_b[:28])
    after = diary.store.read_entries(fmt_normal())
    check("写入后变 6 段", len(after) == 6, f"实得 {len(after)} 段")

    # ---- 3. 按人隔离：/看日记 ----
    print("\n③ /看日记 按人隔离（这是第 17 步要修的那个问题）")
    # ⚠️ 必须查**今天**：新写入落在今天（10-11），A 和 B 各一条，隔离才有得验。
    # 查旧那天（10-04）验的是"历史段落的归属回填"，那是另一回事，下面单独查。
    today = datetime.now().astimezone().strftime("%m-%d")
    html_a = await run_card(plugin, ev_a, today)
    html_b = await run_card(plugin, ev_b, today)
    check("A 的卡片里有 A 自己今天那条", "刚写的一条" in html_a)
    check("A 的卡片里**没有** B 的", "小满这边" not in html_a)
    check("B 的卡片里有 B 自己今天那条", "小满这边" in html_b)
    check("B 的卡片里**没有** A 的", "刚写的一条" not in html_b)

    # 历史那天：三个人（希 / 小满 / 群）各留了一段，A 只该看到自己那段
    old_a = await run_card(plugin, ev_a, "10-04")
    check("历史那天：A 看到自己的旧段", "早上的风是凉的" in old_a)
    check("历史那天：A **看不到**小满的旧段", "今天有点闷" not in old_a)
    check("历史那天：A **看不到**群里那句", "算了" not in old_a)

    # ---- 4. 面板三个接口口径一致 ----
    print("\n④ 面板：日历 / 正文 / 卡片按人过滤")
    for who, day, must, must_not in (
        ("1411638634", today, "刚写的一条", "小满这边"),
        ("u_2002", today, "小满这边", "刚写的一条"),
    ):
        listing = webui("diary_list", who=who)
        days = listing["books"][0]["days"]
        check(f"日历({who}) 只数这个人的天", all(d["date"] for d in days), f"{len(days)} 天")
        content = webui("diary_content", who=who, book="normal", tail="50")
        texts = [e["text"] for e in content["entries"]]
        check(f"正文({who}) 含自己的", any(must in t for t in texts))
        check(f"正文({who}) 不含别人的", not any(must_not in t for t in texts))
        card = webui("diary_card", who=who, book="normal", date=day)
        check(f"卡片({who}) 含自己的", must in card["html"])
        check(f"卡片({who}) 不含别人的", must_not not in card["html"])

    # ---- 5. 回收站往返 ----
    print("\n⑤ 回收站：删 → 还原")
    target = [e for e in diary.store.read_entries(fmt_normal()) if "刚写的一条" in e.text][0]
    trash_id = await run_delete(plugin, ev_a, target)
    check("删除后回收站有 1 条", len(diary.store.trash_items()) == 1)
    gone = [e for e in diary.store.read_entries(fmt_normal()) if "刚写的一条" in e.text]
    check("正文里已经没了", not gone)
    ok = await run_restore(plugin, ev_a, trash_id)
    check("还原成功", ok)
    back = [e for e in diary.store.read_entries(fmt_normal()) if "刚写的一条" in e.text]
    check("内容回来了", len(back) == 1)
    check("归属没丢", bool(back and back[0].person_id == "1411638634"))

    # ---- 6. 把卡片 HTML 落盘，供人眼过 ----
    print("\n⑥ 卡片 HTML 落盘（给人眼过）")
    for style in ("paper", "tape", "seal"):
        page = webui("diary_card", who="1411638634", book="normal", date=today, style=style)
        target_file = OUT_DIR / f"card_{style}.html"
        target_file.write_text(page["html"], encoding="utf-8")
        check(f"{style} 已落盘", target_file.stat().st_size > 500, f"{target_file.stat().st_size} 字节")

    print(f"\n{'=' * 46}")
    if fails:
        print(f"✗ 端到端不过：{len(fails)} 项 —— " + "；".join(fails))
        return 1
    print("✓ 端到端全过")
    print(f"  卡片预览：{OUT_DIR / 'index.html'}")
    return 0


# ---- 小工具 -------------------------------------------------------------------


def fmt_normal() -> str:
    from core.diary import format as fmt

    return fmt.NORMAL


def _json_ok(line: str) -> bool:
    import json

    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def webui(endpoint: str, **params) -> dict:
    from core import storage, webui_data
    from core import settings as S

    st = S.load_settings({"timezone": "Asia/Shanghai", "name_preference": {"1411638634": "希"}})
    layout = storage.Layout(OUT_DIR / "data").ensure()
    store = _Store(layout=layout)
    fn = {
        "diary_list": lambda: webui_data.diary_list_payload(
            diary_store=store, person=params.get("who", "")),
        "diary_content": lambda: webui_data.diary_content_payload(
            diary_store=store, book=params.get("book", "normal"),
            date=params.get("date", ""), tail=int(params.get("tail", 20)),
            settings=st, person=params.get("who", "")),
        "diary_card": lambda: webui_data.diary_card_payload(
            diary_store=store, settings=st, book=params.get("book", "normal"),
            date=params.get("date", ""), style=params.get("style", ""),
            person=params.get("who", ""),
            now=datetime(2026, 10, 11, 12, 0, tzinfo=datetime.now().astimezone().tzinfo)),
    }[endpoint]
    return fn()


class _Store:
    """薄壳：让 webui_data 拿到真实 DiaryStore。"""

    def __init__(self, layout) -> None:
        from core.diary.store import DiaryStore

        self._s = DiaryStore(layout=layout, locks=__import__(
            "core.storage", fromlist=["storage"]
        ).KeyedLocks())

    def __getattr__(self, item):
        return getattr(self._s, item)


async def run_card(plugin, event, date: str) -> str:
    """真跑一次 /看日记，抓下渲染出来的那段 HTML。"""
    captured: dict = {}

    async def fake_render(tmpl, data, return_url=True, options=None):
        from core.diary import card

        captured["html"] = card.render(tmpl, data)
        captured["options"] = options
        return "card.png"

    plugin.html_render = fake_render
    out = await _collect(plugin.diary_card(event, date))
    return captured.get("html", "")


async def run_delete(plugin, event, target) -> str:
    out = await _collect(plugin.diary_edit(event, action="delete", match=target.text[:6]))
    # diary_edit 直接 return **字符串**（不是 (kind, text) 元组），两种都要收
    text = "\n".join(
        str(x[1]) if isinstance(x, tuple) and len(x) > 1 else str(x) for x in out
    )
    items = plugin.diary.store.trash_items()
    if "删掉了" in text and items:
        return str(items[-1].get("id") or "")
    return ""


async def run_restore(plugin, event, trash_id: str) -> bool:
    from core import webui_data

    layout = plugin.layout
    result = await webui_data.restore_diary_entry(
        diary_store=plugin.diary.store,
        settings=plugin.settings,
        trash_id=trash_id,
        now=datetime.now().astimezone(),
    )
    return bool(result.get("ok"))


async def _collect(coro):
    """收干一个可能返回**协程**、也可能返回 **async 生成器** 的 handler。

    ``diary_card`` / ``diary_write`` 是 async 生成器（``yield event.…``），
    ``diary_edit`` 是普通协程（直接 return 一段文字）——两种形状都得收。
    """
    import inspect

    if inspect.isasyncgen(coro):
        return [item async for item in coro]
    return [await coro]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))