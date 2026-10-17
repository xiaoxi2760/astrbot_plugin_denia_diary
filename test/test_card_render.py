"""出图链路（第 20 步）的测试：缓存键、落盘缓存、降级编排、本地探测。

为什么这三条要盯死——它们直接决定 ``/看日记`` 快不快、以及**坏了会不会被用户看见**：

* **缓存键**算错＝改了日记还发旧图（用户改了正文，看到的还是上一版）＝比慢严重得多；
* **降级链**断一环＝出图直接失败，用户看到的是"日记卡不出来"而不是退回文字；
* **缓存写不进去**必须只影响速度、不影响出图（这是 ``card_cache.put`` 存在的全部理由）。

本地出图那条路**不真跑浏览器**（这台机器有没有浏览器不是测试该关心的，而且那要 1.4 秒
冷启动）。做法是把 ``card_local.render`` 换成一个假实现，于是"编排选哪条路"这件事被完整
验到，而"真的截图"那部分由 ``tools/bench_card.py`` 人工验。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import TmpDirCase  # noqa: E402

from core import storage  # noqa: E402
from core.diary import card_cache, card_local, card_render  # noqa: E402
from core.diary import card as card_mod  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfake"


def ctx(text: str = "今天风不大。", date: str = "2026-10-13") -> dict:
    return card_mod.card_context(
        date=date,
        weekday="周二",
        book_name="普通日记",
        entries=[{"time": "20:00", "mood": "安心", "who": "和某某", "paragraphs": [text]}],
        page_index=3,
        page_total=9,
    )


class FakeLocal:
    """假本地渲染器：把 HTML 原样当成 PNG 写下去，并记账调用次数。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def render(self, html_text: str, dest: Path) -> Path:
        self.calls.append((html_text, str(dest)))
        if self.fail:
            raise card_local.LocalRenderError("假装这台机器没浏览器")
        Path(dest).write_bytes(PNG)
        return Path(dest)


class FakeRemote:
    """假远端渲染器：记账并返回一个"已经出好的文件"。"""

    def __init__(self, root: Path, *, fail: bool = False) -> None:
        self.root = root
        self.fail = fail
        self.calls: list[dict] = []
        self.out = root / "remote.png"
        self.out.write_bytes(PNG)

    async def __call__(self, tmpl, data, return_url=True, options=None):
        self.calls.append(
            {"tmpl": tmpl, "data": data, "return_url": return_url, "options": options}
        )
        if self.fail:
            raise RuntimeError("假装远端端点不通")
        return str(self.out)


class PatchLocal:
    """把 ``card_local.render`` 换成假实现（只在这一轮测试里生效）。

    ⚠️ 替换的是**函数**，不是整个模块：``card_render._local`` 用
    ``except card_local.LocalRenderError`` 捕获失败，替身要是把模块换掉，这个异常类就没了，
    降级链会在最底下断——而那正是这套测试要盯着的东西。
    """

    def __init__(self, fake: FakeLocal) -> None:
        self.fake = fake
        self._saved = None

    def __enter__(self):
        self._saved = card_local.render
        card_local.render = self.fake.render  # type: ignore[assignment]
        return self.fake

    def __exit__(self, *exc):
        card_local.render = self._saved  # type: ignore[assignment]
        return False


# ---- 缓存键 -------------------------------------------------------------------


class TestCacheKey(TmpDirCase):
    def template(self, name: str = "paper.html.j2") -> Path:
        path = self.root / name
        path.write_text("<html>{{ date }}</html>", encoding="utf-8")
        return path

    def test_same_input_gives_same_key(self) -> None:
        tmpl = self.template()
        self.assertEqual(
            card_cache.key_for("paper", ctx(), tmpl), card_cache.key_for("paper", ctx(), tmpl)
        )

    def test_style_changes_key(self) -> None:
        """同一份数据，纸感和墨信是两张完全不同的图，不能共用一张缓存。"""
        tmpl = self.template()
        self.assertNotEqual(
            card_cache.key_for("paper", ctx(), tmpl), card_cache.key_for("ink", ctx(), tmpl)
        )

    def test_body_change_changes_key(self) -> None:
        """正文改一个字必须换键——否则用户改完看到的还是旧图。"""
        tmpl = self.template()
        self.assertNotEqual(
            card_cache.key_for("paper", ctx("今天风不大。"), tmpl),
            card_cache.key_for("paper", ctx("今天风很大。"), tmpl),
        )

    def test_date_change_changes_key(self) -> None:
        tmpl = self.template()
        self.assertNotEqual(
            card_cache.key_for("paper", ctx(date="2026-10-13"), tmpl),
            card_cache.key_for("paper", ctx(date="2026-10-14"), tmpl),
        )

    def test_page_number_change_changes_key(self) -> None:
        """页码印在卡上——数出来的页数变了，图就该重出。"""
        tmpl = self.template()
        a = card_mod.card_context(date="d", weekday="w", book_name="b",
                                  entries=[{"time": "1", "mood": "", "who": "",
                                            "paragraphs": ["x"]}], page_index=1, page_total=2)
        b = card_mod.card_context(date="d", weekday="w", book_name="b",
                                  entries=[{"time": "1", "mood": "", "who": "",
                                            "paragraphs": ["x"]}], page_index=2, page_total=2)
        self.assertNotEqual(card_cache.key_for("paper", a, tmpl), card_cache.key_for("paper", b, tmpl))

    def test_template_edit_changes_key(self) -> None:
        """改了 CSS 必须失效重出，否则会把一张改坏了的图反复发出去。"""
        tmpl = self.template()
        before = card_cache.key_for("paper", ctx(), tmpl)
        tmpl.write_text("<html>改了排版 {{ date }}</html>", encoding="utf-8")
        # mtime_ns 精度足够，但文件系统可能太粗——改字节数保证一定变了。
        after = card_cache.key_for("paper", ctx(), tmpl)
        self.assertNotEqual(before, after)

    def test_key_is_stable_across_dict_ordering(self) -> None:
        """ctx 是 dict，键序不该影响结果（否则同一天不同次请求算出两个键，缓存永远不中）。"""
        tmpl = self.template()
        data = ctx()
        shuffled = dict(reversed(list(data.items())))
        self.assertEqual(
            card_cache.key_for("paper", data, tmpl), card_cache.key_for("paper", shuffled, tmpl)
        )

    def test_key_for_html_tracks_content(self) -> None:
        self.assertEqual(card_cache.key_for_html("<p>a</p>"), card_cache.key_for_html("<p>a</p>"))
        self.assertNotEqual(card_cache.key_for_html("<p>a</p>"), card_cache.key_for_html("<p>b</p>"))

    def test_key_for_html_salt_separates_styles(self) -> None:
        """同一段 HTML 换款式时不该顶掉上一张（salt 就是给这个用的）。"""
        self.assertNotEqual(
            card_cache.key_for_html("<p>a</p>", salt="paper"),
            card_cache.key_for_html("<p>a</p>", salt="ink"),
        )

    def test_missing_template_still_yields_a_key(self) -> None:
        """模板读不到也照样能算键——出图会另外走降级，不能在算键这一步就炸。"""
        self.assertTrue(card_cache.key_for("paper", ctx(), self.root / "不存在.j2"))


# ---- 落盘缓存 -----------------------------------------------------------------


class TestCacheStore(TmpDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.dir = self.root / "card-cache"

    def test_store_then_resolve_round_trips(self) -> None:
        self.assertIsNotNone(card_cache.store(self.dir, "k1", PNG))
        hit = card_cache.resolve(self.dir, "k1")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.read_bytes(), PNG)

    def test_resolve_misses_for_unknown_key(self) -> None:
        self.assertIsNone(card_cache.resolve(self.dir, "没有这个键"))

    def test_resolve_misses_on_empty_file(self) -> None:
        """零字节的 PNG 也是"合法文件"，但它不是一张能看的图，必须当没命中。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "k1.png").write_bytes(b"")
        self.assertIsNone(card_cache.resolve(self.dir, "k1"))

    def test_store_refuses_empty_payload(self) -> None:
        self.assertIsNone(card_cache.store(self.dir, "k1", b""))

    def test_put_returns_an_existing_file_even_when_cache_unwritable(self) -> None:
        """缓存目录写不进去也**必须**给出一张能发出去的图（退到系统临时目录）。

        这是 put 存在的全部理由：缓存是加速器，缓存坏了不该让她读不到日记。
        """
        blocked = self.root / "blocked"
        blocked.mkdir()
        os.chmod(blocked, 0o500)  # 只读：mkdir 得进去、写入得失败
        try:
            result = card_cache.put(blocked, "k1", PNG)
            if result is None:
                self.skipTest("这个环境里 root 能绕过只读权限")
            self.assertTrue(result.is_file())
            self.assertEqual(result.read_bytes(), PNG)
        finally:
            os.chmod(blocked, 0o700)

    def test_prune_drops_the_oldest_beyond_the_cap(self) -> None:
        """超上限时按 mtime 从最旧开始删：留最新的 MAX_FILES 张，最旧的那批出局。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        total = card_cache.MAX_FILES + 5
        for i in range(total):
            path = self.dir / f"k{i:03d}.png"
            path.write_bytes(PNG)
            stamp = time.time_ns() - (total - i) * 1_000_000_000  # 编号越大越新
            os.utime(path, ns=(stamp, stamp))
        card_cache.store(self.dir, "trigger", PNG)  # 再写一张，触发一次淘汰
        left = {p.name for p in self.dir.glob("*.png")}
        self.assertEqual(len(left), card_cache.MAX_FILES)
        self.assertNotIn("k000.png", left, "最旧的那张该被淘汰")
        self.assertIn(f"k{total - 1:03d}.png", left, "最新的一张必须留着")

    def test_prune_spares_a_card_that_was_just_viewed(self) -> None:
        """resolve 会刷新 mtime，于是"最近真翻过"的卡在淘汰时排到最后，不会被扫掉。

        这条是 LRU 的全部意义：用户反复翻的那几天，得一直留在缓存里。
        """
        keeper = card_cache.store(self.dir, "keeper", PNG)
        self.assertIsNotNone(keeper)
        # 先刚好填满上限（不触发淘汰），其余都是比 keeper 老的
        old_stamp = time.time_ns() - 3600 * 1_000_000_000
        for i in range(card_cache.MAX_FILES - 1):
            path = card_cache.store(self.dir, f"f{i:03d}", PNG)
            os.utime(path, ns=(old_stamp, old_stamp))

        # 翻一次 → keeper 的 mtime 刷新成"现在"，变成最新的一张
        time.sleep(0.02)
        self.assertIsNotNone(card_cache.resolve(self.dir, "keeper"))

        # 再挤掉 5 张：它们比 keeper 老，淘汰该动它们、不该动 keeper
        for i in range(5):
            path = card_cache.store(self.dir, f"g{i:03d}", PNG)
            os.utime(path, ns=(old_stamp + (i + 1) * 1_000, old_stamp + (i + 1) * 1_000))

        self.assertTrue(keeper.is_file(), "刚翻过的那张必须在淘汰后还在")


# ---- 降级编排 -----------------------------------------------------------------


class TestRenderChain(TmpDirCase):
    """降级编排。**三个引擎全部换成假实现**——这台机器上 playwright 与 pil 都真的能用，
    不换掉的话测到的不是编排，是"这台机器装了什么"。"""

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root / "card-cache"
        self.html = "<html><body>卡片</body></html>"
        self.calls: list[str] = []

    def run_chain(
        self,
        *,
        engine: str,
        key: str = "k1",
        playwright: bool = True,
        remote: bool = True,
        pil: bool = True,
        ctx_data: dict | None = None,
        spies: dict | None = None,
        allow_remote: bool | None = True,
        pass_policy: bool = True,
    ):
        """跑一次编排。``playwright/remote/pil=False`` 表示"那条路确定出不了图"。

        ``allow_remote`` 缺省**开**：这组降级测试盯的是"谁先谁后"，与隐私策略无关，
        所以默认放行远端好让 ``remote`` 那格是通的。隐私那一侧由
        :class:`TestRemotePolicy` 单独盯死（默认关）。
        ``pass_policy=False`` 用来模拟**还没接上策略的旧调用方**（压根不传这个参数）。
        """
        ok = {"playwright": playwright, "remote": remote, "pil": pil}
        self.remote = FakeRemote(self.root)
        self.remote.fail = not remote

        def fake(name: str):
            async def render(html_text, ctx, cache_dir, cache_key, remote_cb):
                self.calls.append(name)
                if spies is not None:
                    spies.setdefault("seen", []).append((name, dict(ctx)))
                if not ok[name]:
                    return None
                if name == "remote":
                    # 真去调远端回调：RENDER_OPTIONS / return_url 这些参数要真看见
                    path = await remote_cb(html_text, ctx, return_url=False,
                                           options=card_mod.RENDER_OPTIONS)
                    self.last_remote_result = path
                    return card_cache.put(cache_dir, cache_key, Path(str(path)).read_bytes())
                return card_cache.put(cache_dir, cache_key, PNG)

            return render

        saved = {n: getattr(card_render, f"render_{n}") for n in card_render.ENGINE_ORDER}
        for name in card_render.ENGINE_ORDER:
            setattr(card_render, f"render_{name}", fake(name))
        try:

            async def go():
                extra = {"allow_remote": allow_remote} if pass_policy else {}
                return await card_render.render_html(
                    self.html,
                    ctx() if ctx_data is None else ctx_data,
                    cache_dir=self.cache, key=key,
                    engine=engine, remote=self.remote, **extra,
                )

            return asyncio.run(go())
        finally:
            for name, original in saved.items():
                setattr(card_render, f"render_{name}", original)

    # ---- 缓存 ---------------------------------------------------------------

    def test_cache_hit_skips_every_engine(self) -> None:
        """命中缓存＝出图这件事整个跳过：三个引擎一个都不该被叫。"""
        card_cache.store(self.cache, "k1", PNG)
        result = self.run_chain(engine="auto")
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, [], "命中缓存不该再花 4~6 秒去出图")

    def test_result_is_written_into_cache_for_next_time(self) -> None:
        first = self.run_chain(engine="auto", key="k9")
        self.assertIsNotNone(first)
        self.assertIsNotNone(card_cache.resolve(self.cache, "k9"))
        self.calls.clear()
        second = self.run_chain(engine="auto", key="k9")
        self.assertIsNotNone(second)
        self.assertEqual(self.calls, [], "第二次该命中缓存，不该再出一次图")

    # ---- 选谁 ---------------------------------------------------------------

    def test_auto_prefers_playwright(self) -> None:
        self.assertIsNotNone(self.run_chain(engine="auto"))
        self.assertEqual(self.calls, ["playwright"], "auto 首选最快最贴模板的那条")

    def test_explicit_engine_is_tried_first(self) -> None:
        self.assertIsNotNone(self.run_chain(engine="remote"))
        self.assertEqual(self.calls, ["remote"], "选了就该先试它")
        self.calls.clear()
        self.assertIsNotNone(self.run_chain(engine="pil", key="k2"))
        self.assertEqual(self.calls, ["pil"])

    def test_engine_choice_does_not_worse_the_result(self) -> None:
        """选了别的引擎不代表别的就不试了——降级梯是共享的。"""
        self.assertIsNotNone(self.run_chain(engine="remote", remote=False))
        self.assertEqual(self.calls, ["remote", "playwright"],
                         "remote 挂了该退到 playwright，而不是直接没图")

    # ---- 降级 ---------------------------------------------------------------

    def test_ladder_order_is_playwright_then_remote_then_pil(self) -> None:
        """全挂时按 playwright → remote → pil 的顺序退一遍。"""
        self.assertIsNone(
            self.run_chain(engine="auto", playwright=False, remote=False, pil=False)
        )
        self.assertEqual(self.calls, ["playwright", "remote", "pil"])

    def test_pil_is_the_last_resort(self) -> None:
        """前两条都挂 → PIL 接住。这是加 PIL 的全部意义：出图永远有条底。"""
        result = self.run_chain(engine="auto", playwright=False, remote=False)
        self.assertIsNotNone(result)
        self.assertEqual(self.calls[-1], "pil")

    def test_all_three_failing_returns_none(self) -> None:
        self.assertIsNone(self.run_chain(engine="auto", playwright=False, remote=False, pil=False))

    def test_failure_never_raises(self) -> None:
        """编排层只返回 None，不往外抛——抛了就把调用方的降级链一起带崩。"""
        self.assertIsNone(self.run_chain(engine="pil", pil=False, playwright=False, remote=False))

    # ---- 参数 ---------------------------------------------------------------

    def test_remote_gets_same_render_options_as_chat_card(self) -> None:
        """远端出图必须用同一份 RENDER_OPTIONS + return_url=False，否则两张图不一样。"""
        self.run_chain(engine="remote")
        self.assertEqual(len(self.remote.calls), 1)
        self.assertEqual(self.remote.calls[0]["options"], card_mod.RENDER_OPTIONS)
        self.assertFalse(self.remote.calls[0]["return_url"])

    def test_every_engine_sees_the_same_context(self) -> None:
        """每个引擎拿到的必须是同一份数据——否则换引擎等于换了一张卡。"""
        spies: dict = {}
        self.run_chain(engine="auto", playwright=False, key="k3", spies=spies)
        self.assertEqual([name for name, _ in spies["seen"]], ["playwright", "remote"])
        self.assertEqual(spies["seen"][0][1], spies["seen"][1][1])
        self.assertTrue(spies["seen"][0][1].get("entries"), "得是真正有内容的 ctx")

    def test_pil_is_handed_the_same_ctx_it_would_have_drawn(self) -> None:
        """PIL 画的是 ctx：它拿到的必须和 playwright 那边一模一样，换引擎才换得出同一张卡。"""
        spies: dict = {}
        self.run_chain(engine="pil", key="k4", playwright=False, remote=False, spies=spies)
        handed = [data for name, data in spies["seen"] if name == "pil"][0]
        self.assertEqual(handed, ctx())


class TestRemotePolicy(TmpDirCase):
    """**隐私闸门** ``allow_remote``：禁止上传时，remote 这条路整条消失。

    这一组盯的是本任务最要紧的那句话：**"禁止远端时任何引擎失败都不能上传"**。所以断言一律
    看**远端回调有没有被调用过**，而不是"最后出了什么图"——最后出了图（本机出的）是对的，
    但如果 remote 在中间被叫过，那张图的内容就已经躺在别人机器上了。
    """

    def setUp(self) -> None:
        super().setUp()
        self.cache = self.root / "card-cache"
        self.html = "<html><body>卡片</body></html>"

    def run_chain(self, *, engine: str, key: str = "k1", ok: dict | None = None,
                  allow_remote: bool | None = False, pass_policy: bool = True):
        """跑一次真编排。``ok`` 决定哪条引擎"能出图"（默认只有 playwright 能）。"""
        ok = {"playwright": True, "remote": True, "pil": True} if ok is None else ok
        remote_spy = FakeRemote(self.root)

        def fake(name: str):
            async def render(html_text, ctx, cache_dir, cache_key, remote_cb):
                self.calls.append(name)
                if not ok.get(name, False):
                    return None
                if name == "remote":
                    # 真去调远端回调：这样 spy 才可能记录到"上传发生过"。
                    # 禁远端时 remote 压根不在梯子里，这行一次都不会走到——
                    # 断言 spy.calls == [] 验的正是这一点。
                    return card_cache.put(
                        cache_dir, cache_key,
                        Path(str(await remote_cb(html_text, ctx, return_url=False,
                                                 options=card_mod.RENDER_OPTIONS))).read_bytes(),
                    )
                return card_cache.put(cache_dir, cache_key, PNG)

            return render

        self.calls = []
        saved = {n: getattr(card_render, f"render_{n}") for n in card_render.ENGINE_ORDER}
        for name in card_render.ENGINE_ORDER:
            setattr(card_render, f"render_{name}", fake(name))
        try:
            extra = {"allow_remote": allow_remote} if pass_policy else {}

            async def go():
                return await card_render.render_html(
                    self.html, ctx(),
                    cache_dir=self.cache, key=key,
                    engine=engine, remote=remote_spy, **extra,
                )

            result = asyncio.run(go())
        finally:
            for name, original in saved.items():
                setattr(card_render, f"render_{name}", original)
        return result, remote_spy

    # ---- 拒绝远端 -------------------------------------------------------------

    def test_denied_remote_is_never_called_when_playwright_works(self) -> None:
        result, spy = self.run_chain(engine="auto", allow_remote=False)
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, ["playwright"])
        self.assertEqual(spy.calls, [], "禁止上传时远端回调一次都不能被叫")

    def test_denied_remote_is_skipped_on_engine_failure(self) -> None:
        """本机引擎失败时**降级到 pil**，不是降级到 remote。"""
        result, spy = self.run_chain(
            engine="auto", ok={"playwright": False, "remote": True, "pil": True},
            allow_remote=False,
        )
        self.assertIsNotNone(result, "本机还有 pil 就该出图，不该因为禁远端就整条断掉")
        self.assertEqual(self.calls, ["playwright", "pil"])
        self.assertEqual(spy.calls, [], "引擎失败不许偷偷改成上传")

    def test_denied_remote_all_local_fail_returns_none_instead_of_uploading(self) -> None:
        """本机全挂 → 返回 None 交给调用方退文字，**绝不**退到远端。"""
        result, spy = self.run_chain(
            engine="auto", ok={"playwright": False, "remote": True, "pil": False},
            allow_remote=False,
        )
        self.assertIsNone(result)
        self.assertEqual(self.calls, ["playwright", "pil"])
        self.assertEqual(spy.calls, [], "出不了图就退文字，不能拿日记去换一张图")

    def test_explicit_remote_engine_conflicts_with_deny(self) -> None:
        """配置选了 remote 但禁止上传：**以禁止为准**，本机引擎接手。"""
        result, spy = self.run_chain(engine="remote", allow_remote=False)
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, ["playwright"], "冲突时该降级到本机，而不是先试 remote")
        self.assertEqual(spy.calls, [])

    def test_denied_auto_with_explicit_remote_engine_leaves_only_local_engines(self) -> None:
        """auto + 禁止上传：梯子里压根不该有 remote 这一格。"""
        ladder = card_render._ladder("auto", allow_remote=False)
        self.assertNotIn("remote", ladder)
        self.assertEqual(ladder, ("playwright", "pil"))

    def test_ladder_never_goes_empty_when_remote_is_denied(self) -> None:
        """禁远端不等于不能出图：梯子必须还剩本机引擎（最差退回文字，不至于全灭）。"""
        for engine in card_render.ENGINE_ORDER + ("auto", "乱填的值"):
            with self.subTest(engine=engine):
                self.assertTrue(card_render._ladder(engine, allow_remote=False))

    # ---- 漏接线 ---------------------------------------------------------------

    def test_missing_policy_argument_is_treated_as_denied(self) -> None:
        """调用方压根没传这个参数 → 按**禁止**处理，且远端一次都不许被叫。"""
        result, spy = self.run_chain(
            engine="auto", ok={"playwright": False, "remote": True, "pil": True},
            allow_remote=None, pass_policy=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, ["playwright", "pil"])
        self.assertEqual(spy.calls, [], "漏传只能少出图，绝不能变成上传")

    def test_missing_policy_argument_on_explicit_remote_engine(self) -> None:
        _, spy = self.run_chain(engine="remote", allow_remote=None, pass_policy=False)
        self.assertEqual(spy.calls, [])
        self.assertNotIn("remote", self.calls)

    # ---- 显式允许 -------------------------------------------------------------

    def test_explicitly_allowed_still_reaches_remote(self) -> None:
        """显式允许时行为和以前一模一样：remote 还在梯子里，且能被选中。"""
        result, spy = self.run_chain(
            engine="auto", ok={"playwright": False, "remote": True, "pil": False},
            allow_remote=True,
        )
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, ["playwright", "remote"])
        self.assertEqual(len(spy.calls), 1, "允许远端时 remote 该照常被叫")

    def test_allowed_ladder_keeps_remote_in_place(self) -> None:
        self.assertEqual(
            card_render._ladder("auto", allow_remote=True), card_render.ENGINE_ORDER
        )
        self.assertEqual(
            card_render._ladder("remote", allow_remote=True),
            ("remote", "playwright", "pil"),
        )

    # ---- 缓存不能绕过策略 -----------------------------------------------------

    def test_denied_policy_does_not_reuse_a_remote_made_cache_entry(self) -> None:
        """关掉远端后，之前远端出的那张图**不该**被命中发出去。"""
        card_cache.store(self.cache, "k1", PNG)  # 允许远端时的键：没后缀
        result, spy = self.run_chain(engine="auto", key="k1", allow_remote=False)
        self.assertEqual(self.calls, ["playwright"], "该重出一张本机图，不是拿旧缓存顶上")
        self.assertIsNotNone(result)
        self.assertEqual(spy.calls, [])

    def test_allowed_policy_reuses_the_unsuffixed_cache_entry(self) -> None:
        """允许远端时键不变：老用户升级后本机已有的缓存照样命中（不必全体重出）。"""
        card_cache.store(self.cache, "k1", PNG)
        result = self.run_chain(engine="auto", key="k1", allow_remote=True)[0]
        self.assertIsNotNone(result)
        self.assertEqual(self.calls, [], "允许远端时键就是原键，该直接命中")

    def test_the_two_policies_do_not_share_cache_entries(self) -> None:
        """两种策略下的产物互不顶替：禁远端那张不会冒充"远端出的那张"。"""
        card_cache.store(self.cache, "k1-localonly", PNG)
        result, spy = self.run_chain(engine="auto", key="k1", allow_remote=True)
        self.assertEqual(self.calls, ["playwright"], "远端策略不该捡起禁远端时出的那张")
        self.assertIsNotNone(result)
        self.assertEqual(spy.calls, [])

    def test_denied_policy_caches_under_its_own_key(self) -> None:
        self.run_chain(engine="auto", key="k7", allow_remote=False)
        self.assertIsNotNone(card_cache.resolve(self.cache, "k7-localonly"))
        self.assertIsNone(card_cache.resolve(self.cache, "k7"))


class _null:
    """空上下文（本地那档压根没被用到时用不着造假实现）。"""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---- 本地能力探测 -------------------------------------------------------------


class _NoBrowser:
    """临时把"这台机器一个浏览器都没有"造出来（改候选表 + 指死环境变量）。

    ⚠️ 这里动的是 ``card_local`` 的模块级候选表，不是环境——测试跑在真实机器上，
    直接把 ``DIARY_CARD_BROWSER`` 指到不存在的路径就够（``find_browser`` 把指错的
    环境变量当"没设置"，于是候选表说了算）。
    """

    def __enter__(self):
        self._saved_env = os.environ.get(card_local.BROWSER_ENV)
        self._saved_paths = card_local._BROWSER_CANDIDATES
        os.environ[card_local.BROWSER_ENV] = str(Path("Z:/根本没有这个浏览器.exe"))
        card_local._BROWSER_CANDIDATES = ()
        return self

    def __exit__(self, *exc):
        card_local._BROWSER_CANDIDATES = self._saved_paths
        if self._saved_env is None:
            os.environ.pop(card_local.BROWSER_ENV, None)
        else:
            os.environ[card_local.BROWSER_ENV] = self._saved_env
        return False


class TestLocalProbe(unittest.TestCase):
    def test_probe_returns_reason_never_raises(self) -> None:
        """探测必须便宜且绝不抛：它决定要不要起浏览器，抛异常就等于整条路废了。"""
        ok, reason = card_local.probe()
        self.assertIsInstance(ok, bool)
        self.assertTrue(reason.strip(), "无论能不能，理由都要有——排查时全靠它")

    def test_env_override_pointing_nowhere_does_not_raise(self) -> None:
        saved = os.environ.get(card_local.BROWSER_ENV)
        os.environ[card_local.BROWSER_ENV] = str(Path("Z:/根本没有这个浏览器.exe"))
        try:
            self.assertIsNone(card_local.find_browser())
            ok, reason = card_local.probe()
            self.assertFalse(ok, "指向不存在的文件时该当没设置，继续找系统浏览器")
            self.assertTrue(reason)
        finally:
            if saved is None:
                os.environ.pop(card_local.BROWSER_ENV, None)
            else:
                os.environ[card_local.BROWSER_ENV] = saved

    def test_render_without_browser_raises_local_error(self) -> None:
        """出不了图必须抛 :class:`LocalRenderError`（调用方只认这一种）。

        不能把 playwright 的 ImportError 或超时原样漏上去——编排层只捕 ``LocalRenderError``，
        漏了别的异常就等于把"降级"这条链在最底下断掉。
        """
        with _NoBrowser():
            with self.assertRaises(card_local.LocalRenderError):
                asyncio.run(card_local.render("<p>x</p>", Path("unused.png")))


class TestLayoutKnowsCardCache(TmpDirCase):
    def test_card_cache_lives_under_data_dir(self) -> None:
        layout = storage.Layout(self.root)
        self.assertEqual(layout.card_cache, self.root / "card-cache")

    def test_card_cache_is_not_a_diary_file(self) -> None:
        """缓存是纯派生数据，不进 files()——备份/导出不该把它当日记的一部分。"""
        layout = storage.Layout(self.root).ensure()
        self.assertNotIn(layout.card_cache, layout.files())

    def test_ensure_does_not_require_card_cache_dir(self) -> None:
        """ensure() 不预建缓存目录：它是惰性写入的产物，空目录留着只是让人以为"有内容"。"""
        layout = storage.Layout(self.root).ensure()
        self.assertFalse(layout.card_cache.exists())


class TestRealBrowserSmoke(TmpDirCase):
    """真起一次浏览器的冒烟测试——**只在探测得到浏览器时跑**，否则 skip。

    为什么值得单独一条：上面所有用例都把 ``card_local.render`` 换成了假实现，而这个模块
    恰恰有一类 bug 是假实现**照不到**的（真实浏览器 API 用错、await 漏了、启动参数不对）。
    实际踩过一次：``_browser`` 这个名字同时是访问函数和单例变量，第一次调用把函数自己
    当浏览器返回，所有单测全绿、真跑才炸。**所以这里必须有一条不替换任何东西的用例。**

    每条用例都在**同一个事件循环**里出图并关闭浏览器：playwright 的浏览器对象绑在创建它
    的那个循环上，跨循环关它只会得到一屏 ``closed pipe``（插件正常运行时也只有一个循环，
    ``terminate`` 与出图都在它上面）。
    """

    def setUp(self) -> None:
        super().setUp()
        ok, reason = card_local.probe()
        if not ok:
            self.skipTest(f"这台机器不能本地出图，跳过真浏览器冒烟：{reason}")
        self._html = card_mod.render_card("paper", ctx("今天风不大，窗台上那盆绿萝又长高了一点。"))

    def test_renders_real_png_through_the_whole_chain(self) -> None:
        dest = self.root / "real.png"

        async def go():
            try:
                return await card_local.render(self._html, dest)
            finally:
                await card_local.shutdown()

        self.assertEqual(asyncio.run(go()), dest)
        raw = dest.read_bytes()
        self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"), "产物必须是真 PNG，不能是半张")
        self.assertGreater(len(raw), 5_000, f"PNG 小得可疑（{len(raw)} 字节），八成是空白页")

    def test_second_render_reuses_the_same_browser(self) -> None:
        """第二次不再付 1.4 秒的冷启动——常驻浏览器确实被复用了（换掉重启会慢一个数量级）。"""

        async def go():
            try:
                await card_local.render(self._html, self.root / "a.png")
                started = time.perf_counter()
                await card_local.render(self._html, self.root / "b.png")
                return time.perf_counter() - started
            finally:
                await card_local.shutdown()

        warm = asyncio.run(go())
        self.assertLess(warm, 6.0, "热渲染不该再付浏览器冷启动的代价（这次真出成了图）")
        self.assertTrue((self.root / "b.png").is_file())

    def test_engine_recovers_after_a_loop_change(self) -> None:
        """换过事件循环后仍能出图（宿主热重载、换 loop 都可能发生）。

        旧实例属于已经结束的循环，用不了也不该去关——只管丢掉重建。

        ⚠️ 这条用例**故意**留下一个绑在死循环上的孤儿浏览器，所以解释器退出时 asyncio 会在
        ``__del__`` 里吐两行 ``I/O operation on closed pipe``。那是这条用例的**预期产物**，
        不是故障：那个循环已经没了，它的 transport 只能在 GC 时才发现自己是死的。别的用例
        （含上面两条真出图）都是安静的。
        """

        async def first():
            await card_local.render(self._html, self.root / "loop1.png")

        async def second():
            try:
                return await card_local.render(self._html, self.root / "loop2.png")
            finally:
                await card_local.shutdown()

        asyncio.run(first())  # 不 shutdown：留下一个属于这条（已结束）循环的实例
        self.assertEqual(asyncio.run(second()), self.root / "loop2.png")


if __name__ == "__main__":
    unittest.main()