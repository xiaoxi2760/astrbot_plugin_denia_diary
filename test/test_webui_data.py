"""WebUI 数据面离线单测（第 5 步）。

照任务书 §11 的验收方式来核：临时目录里造出各个数据文件 → 调
``core/webui_data`` 的**每个** payload 函数 → 核 §2.1 的**必备键** + 数值正确
（曲线点数、记录倒序、档位词、幂等、不存在的 id 返回结构化错误而不是抛异常）。

数据面**不需要 astrbot、也不需要起服务**——这正是把它放在 ``core/`` 的意义。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import NOW, TmpDirCase  # noqa: E402

from core import settings as settings_mod  # noqa: E402
from core import storage  # noqa: E402
from core import webui_data  # noqa: E402
from core.diary import format as fmt  # noqa: E402
from core.diary.store import DiaryStore  # noqa: E402
from core.notebook.store import NotebookStore  # noqa: E402
from core.proactive.store import ProactiveStore  # noqa: E402
from core.state.store import LAYER_BASELINE, LAYER_NOW, AffinityStore, StateStore  # noqa: E402

TZ = NOW.tzinfo


def make_settings(**overrides) -> settings_mod.Settings:
    raw: dict = {"timezone": "Asia/Shanghai"}
    raw.update(overrides)
    return settings_mod.load_settings(raw)


class WebuiDataCase(TmpDirCase):
    """每个用例一份完整的 store 组合 + 造好的数据文件。"""

    def setUp(self) -> None:
        super().setUp()
        self.settings = make_settings(love_peers=["u_1001"], name_preference={"u_1001": "希"})
        self.layout = storage.Layout(self.root).ensure()
        self.locks = storage.KeyedLocks()
        self.diary_store = DiaryStore(layout=self.layout, locks=self.locks)
        self.notebook_store = NotebookStore(layout=self.layout, locks=self.locks)
        self.state_store = StateStore(layout=self.layout, locks=self.locks)
        self.affinity_store = AffinityStore(layout=self.layout, locks=self.locks)
        self.proactive_store = ProactiveStore(layout=self.layout, locks=self.locks)

    def write_json(self, path: Path, payload: dict) -> None:
        storage.atomic_write_json(path, payload)

    def run_async(self, coro):
        return asyncio.run(coro)


class TestStatusPayload(WebuiDataCase):
    def test_required_keys(self) -> None:
        payload = webui_data.status_payload(
            settings=self.settings,
            layout=self.layout,
            state_store=self.state_store,
            proactive_store=self.proactive_store,
            version="0.4.0",
            now=NOW,
            who="u_1001",
        )
        for key in ("version", "now", "who", "subsystems", "mood", "rhythm", "files", "today", "log_total"):
            self.assertIn(key, payload, f"§2.1 必备键缺了：{key}")
        self.assertEqual(payload["version"], "0.4.0")
        self.assertIsInstance(payload["subsystems"], dict)
        self.assertIsInstance(payload["log_total"], int)
        self.assertIn("diary", payload["subsystems"])
        self.assertIn("now", payload)  # 服务器时刻
        for key in ("word", "valence", "arousal", "updated_at", "baseline"):
            self.assertIn(key, payload["mood"])  # 原样取 State.snapshot 的子表
        for key in ("word", "late_night"):
            self.assertIn(key, payload["rhythm"])
        for key in ("date", "by_session", "total"):
            self.assertIn(key, payload["today"])

    def test_files_report_shape(self) -> None:
        self.write_json(self.layout.notebook, {"facts": {}, "promises": []})
        payload = webui_data.status_payload(
            settings=self.settings,
            layout=self.layout,
            state_store=self.state_store,
            proactive_store=self.proactive_store,
            now=NOW,
        )
        for name in ("日记.txt", "notebook.json", "state_history.jsonl", "proactive-log.jsonl"):
            self.assertIn(name, payload["files"])
        entry = payload["files"]["notebook.json"]
        for key in ("exists", "bytes", "mtime"):
            self.assertIn(key, entry)
        self.assertTrue(entry["exists"])
        self.assertGreater(entry["bytes"], 0)
        self.assertEqual(payload["files"]["日记.txt"]["exists"], False)
        self.assertEqual(payload["files"]["日记.txt"]["bytes"], 0)

    def test_today_counts_only_today_sessions(self) -> None:
        today = NOW.date().isoformat()
        yesterday = (NOW - timedelta(days=1)).date().isoformat()
        self.write_json(self.layout.proactive, {
            "sessions": {
                "platform:qq:private:1": {"today_date": today, "today_count": 2, "last_slot": "greeting"},
                "platform:qq:private:2": {"today_date": yesterday, "today_count": 9, "last_slot": "night"},
            }
        })
        payload = webui_data.status_payload(
            settings=self.settings,
            layout=self.layout,
            state_store=self.state_store,
            proactive_store=self.proactive_store,
            now=NOW,
        )
        self.assertEqual(payload["today"]["date"], today)
        self.assertEqual(payload["today"]["by_session"], 2)
        self.assertEqual(payload["today"]["total"], 2)

    def test_who_options_include_love_peers(self) -> None:
        self.write_json(self.layout.proactive, {"contacts": {"u_2002": {"umo": "x", "kind": "private"}}})
        payload = webui_data.status_payload(
            settings=self.settings,
            layout=self.layout,
            state_store=self.state_store,
            proactive_store=self.proactive_store,
            now=NOW,
            who="u_1001",
        )
        ids = [item["id"] for item in payload["who_options"]]
        self.assertIn("u_1001", ids)  # love_peers 兜底
        self.assertIn("u_2002", ids)  # contacts
        self.assertEqual(payload["who_name"], "希")


class TestDiaryPayloads(WebuiDataCase):
    def seed_diaries(self) -> None:
        storage.atomic_write_text(
            self.layout.diary,
            "2026-10-01 21:30（开心）〔希〕\n普通日记一。\n\n"
            "2026-10-03 20:00（安心）\n普通日记二。\n\n"
            "2026-10-04 09:10\n普通日记三。\n",
        )
        storage.atomic_write_text(
            self.layout.love_diary, "2026-10-02 23:40（想念）\n恋爱日记一。\n"
        )

    def test_list_required_keys_and_numbers(self) -> None:
        self.seed_diaries()
        payload = webui_data.diary_list_payload(diary_store=self.diary_store, now=NOW)
        for key in ("ok", "books", "love_collapsed"):
            self.assertIn(key, payload)
        self.assertTrue(payload["love_collapsed"])  # 决策 #18：恒 true
        self.assertEqual([book["book"] for book in payload["books"]], list(fmt.BOOKS))
        normal = payload["books"][0]
        for key in ("book", "display", "is_love", "entries", "chars", "updated_at", "latest_date"):
            self.assertIn(key, normal)
        self.assertEqual(normal["entries"], 3)
        self.assertEqual(normal["latest_date"], "2026-10-04")
        self.assertFalse(normal["is_love"])
        love = payload["books"][1]
        self.assertTrue(love["is_love"])
        self.assertEqual(love["entries"], 1)
        self.assertGreater(love["chars"], 0)

    def test_content_tail_and_date(self) -> None:
        self.seed_diaries()
        tail = webui_data.diary_content_payload(diary_store=self.diary_store, book=fmt.NORMAL, tail=2)
        for key in ("ok", "book", "date", "count", "total", "text"):
            self.assertIn(key, tail)
        self.assertEqual(tail["total"], 3)
        self.assertEqual(tail["count"], 2)
        self.assertIn("普通日记三", tail["text"])
        self.assertNotIn("普通日记一", tail["text"])

        one_day = webui_data.diary_content_payload(
            diary_store=self.diary_store, book=fmt.NORMAL, date="2026-10-01"
        )
        self.assertEqual(one_day["count"], 1)
        self.assertIn("普通日记一", one_day["text"])

    def test_content_book_fallback(self) -> None:
        self.seed_diaries()
        payload = webui_data.diary_content_payload(diary_store=self.diary_store, book="不存在的书")
        self.assertEqual(payload["book"], fmt.NORMAL)  # 认不出的书名回落日记

    def test_empty_diary_is_not_an_error(self) -> None:
        payload = webui_data.diary_list_payload(diary_store=self.diary_store, now=NOW)
        self.assertEqual(payload["books"][0]["entries"], 0)
        self.assertEqual(payload["books"][0]["latest_date"], "")


class TestNotebookPayload(WebuiDataCase):
    def seed(self) -> None:
        self.write_json(self.layout.notebook, {
            "facts": {
                "u_1001": [{"id": "f1", "text": "达妮娅不喝咖啡", "created_at": NOW.isoformat()}],
                "u_1002": [{"id": "f2", "text": "达妮娅怕打雷", "created_at": NOW.isoformat()}],
            },
            "promises": [
                {"id": "p1", "about": "u_1001", "text": "一起去看展", "created_at": NOW.isoformat(), "done_at": ""},
                {"id": "p2", "about": "u_1001", "text": "读完那本书", "created_at": NOW.isoformat(),
                 "done_at": NOW.isoformat()},
                {"id": "p3", "about": "u_1002", "text": "带达妮娅看海", "created_at": NOW.isoformat(), "done_at": ""},
            ],
        })

    def test_required_keys_and_who_filter(self) -> None:
        self.seed()
        payload = webui_data.notebook_payload(
            settings=self.settings, notebook_store=self.notebook_store, who="u_1001"
        )
        for key in ("ok", "who", "who_name", "facts", "promises", "limits"):
            self.assertIn(key, payload)
        self.assertEqual(payload["who"], "u_1001")
        self.assertEqual(payload["who_name"], "希")
        self.assertEqual([item["id"] for item in payload["facts"]], ["f1"])
        self.assertEqual({item["id"] for item in payload["promises"]}, {"p1", "p2"})
        # 原样透传 store 条目，键名别改
        self.assertIn("created_at", payload["facts"][0])
        self.assertEqual(payload["limits"]["promise_limit"], 20)

    def test_no_who_returns_everything(self) -> None:
        self.seed()
        payload = webui_data.notebook_payload(settings=self.settings, notebook_store=self.notebook_store)
        self.assertEqual({item["id"] for item in payload["facts"]}, {"f1", "f2"})
        self.assertEqual(len(payload["promises"]), 3)
        ids = [item["id"] for item in payload["who_options"]]
        for expected in ("u_1001", "u_1002"):
            self.assertIn(expected, ids)

    def test_complete_is_idempotent(self) -> None:
        self.seed()
        first = self.run_async(webui_data.complete_note(self.notebook_store, "p1", now=NOW))
        self.assertEqual(first["ok"], True)
        self.assertEqual(first["kind"], "promise")
        self.assertEqual(first["id"], "p1")
        self.assertEqual(first["text"], "一起去看展")

        second = self.run_async(webui_data.complete_note(self.notebook_store, "p1", now=NOW))
        self.assertEqual(second["ok"], False)
        self.assertIn("error", second)
        self.assertNotIn("traceback", str(second).lower())

    def test_complete_missing_id_returns_structured_error(self) -> None:
        self.seed()
        result = self.run_async(webui_data.complete_note(self.notebook_store, "不存在", now=NOW))
        self.assertEqual(result["ok"], False)
        self.assertTrue(str(result.get("error") or ""))
        empty_id = self.run_async(webui_data.complete_note(self.notebook_store, "  ", now=NOW))
        self.assertEqual(empty_id["ok"], False)

    def test_complete_actually_writes_file(self) -> None:
        self.seed()
        self.run_async(webui_data.complete_note(self.notebook_store, "p1", now=NOW))
        storage.shared_cache.invalidate(self.layout.notebook)
        raw = json.loads(self.layout.notebook.read_text(encoding="utf-8"))
        target = [item for item in raw["promises"] if item["id"] == "p1"][0]
        self.assertTrue(target["done_at"])

    def test_forget_moves_to_trash_and_is_idempotent(self) -> None:
        self.seed()
        first = self.run_async(webui_data.forget_note(self.notebook_store, "f1"))
        self.assertEqual(first["ok"], True)
        self.assertEqual(first["kind"], "fact")
        storage.shared_cache.invalidate(self.layout.notebook)
        raw = json.loads(self.layout.notebook.read_text(encoding="utf-8"))
        self.assertNotIn("f1", [item["id"] for item in raw["facts"].get("u_1001", [])])
        self.assertNotIn("u_1001", raw["facts"])  # 空桶不攒空键（store 的行为）
        self.assertEqual(len(raw["trash"]), 1)
        self.assertEqual(raw["trash"][0]["kind"], "fact")

        again = self.run_async(webui_data.forget_note(self.notebook_store, "f1"))
        self.assertEqual(again["ok"], False)
        self.assertIn("error", again)

    def test_forget_promise_kind(self) -> None:
        self.seed()
        result = self.run_async(webui_data.forget_note(self.notebook_store, "p3"))
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["kind"], "promise")


class TestAffinityPayload(WebuiDataCase):
    def test_bands_and_order(self) -> None:
        self.write_json(self.layout.affinity, {"people": {
            "u_1001": {"score": 12.0, "last_ts": NOW.isoformat()},
            "u_1002": {"score": 4.0, "last_ts": (NOW - timedelta(days=1)).isoformat()},
            "u_1003": {"score": 0.5, "last_ts": (NOW - timedelta(days=9)).isoformat()},
            "u_1004": {"score": 0.0, "last_ts": (NOW - timedelta(days=90)).isoformat()},
        }})
        payload = webui_data.affinity_payload(
            settings=self.settings, layout=self.layout, affinity_store=self.affinity_store, now=NOW
        )
        for key in ("ok", "people", "total_known", "love_peers"):
            self.assertIn(key, payload)
        ids = [item["id"] for item in payload["people"]]
        self.assertNotIn("u_1004", ids)  # 衰减后 ≤ 0 不出场
        scores = [item["score"] for item in payload["people"]]
        self.assertEqual(scores, sorted(scores, reverse=True))  # 分数降序
        for item in payload["people"]:
            for key in ("id", "name", "score", "band", "last_ts"):
                self.assertIn(key, item)
            self.assertIn(item["band"], ("熟稔", "泛泛", "陌生"))  # 档位词照抄门面
        self.assertEqual(payload["total_known"], 4)
        self.assertEqual(payload["love_peers"], ["u_1001"])
        self.assertEqual(payload["people"][0]["name"], "希")

    def test_empty(self) -> None:
        payload = webui_data.affinity_payload(
            settings=self.settings, layout=self.layout, affinity_store=self.affinity_store, now=NOW
        )
        self.assertEqual(payload["people"], [])


class TestHistoryPayload(WebuiDataCase):
    def seed_lines(self) -> None:
        lines = [
            {"ts": (NOW - timedelta(days=1)).isoformat(), "layer": LAYER_NOW, "valence": 0.4, "arousal": -0.1, "word": "开心"},
            {"ts": NOW.isoformat(), "layer": LAYER_NOW, "valence": 0.2, "arousal": 0.3, "word": "安心"},
            {"ts": NOW.isoformat(), "layer": LAYER_NOW, "valence": 0.1, "arousal": 0.2, "word": "安心"},
            {"ts": (NOW - timedelta(days=2)).isoformat(), "layer": LAYER_BASELINE, "valence": 0.05, "arousal": 0.0, "word": "平"},
            {"ts": (NOW - timedelta(days=200)).isoformat(), "layer": LAYER_NOW, "valence": -0.9, "arousal": 0.9, "word": "很久以前"},
        ]
        storage.atomic_write_text(
            self.layout.state_history,
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        )

    def test_points_aggregated_by_day_and_layer(self) -> None:
        self.seed_lines()
        payload = webui_data.history_payload(state_store=self.state_store, days=30, now=NOW)
        for key in ("ok", "days", "points", "truncated"):
            self.assertIn(key, payload)
        self.assertEqual(payload["days"], 30)
        # 30 天内：今天 2 条合并成 1 点、昨天 1 点、两天前基调 1 点
        self.assertEqual(len(payload["points"]), 3)
        today_points = [p for p in payload["points"] if p["date"] == NOW.date().isoformat()]
        self.assertEqual(len(today_points), 1)
        self.assertEqual(today_points[0]["n"], 2)
        for point in payload["points"]:
            for key in ("date", "layer", "valence", "arousal", "n"):
                self.assertIn(key, point)
            self.assertIn(point["layer"], (LAYER_NOW, LAYER_BASELINE))
        self.assertEqual(payload["points"], sorted(payload["points"], key=lambda p: (p["date"], p["layer"])))

    def test_truncated_flag_and_window(self) -> None:
        self.seed_lines()
        payload = webui_data.history_payload(state_store=self.state_store, days=30, now=NOW)
        self.assertTrue(payload["truncated"])  # 200 天前那条被窗口裁掉
        wide = webui_data.history_payload(state_store=self.state_store, days=365, now=NOW)
        self.assertFalse(wide["truncated"])
        self.assertEqual(len(wide["points"]), 4)

    def test_bad_lines_are_skipped_not_raised(self) -> None:
        storage.atomic_write_text(
            self.layout.state_history,
            "坏行不是json\n" + json.dumps({"ts": NOW.isoformat(), "layer": LAYER_NOW, "valence": 0.1}) + "\n"
            + json.dumps({"layer": LAYER_NOW, "valence": 0.1}) + "\n",  # 缺 ts
        )
        payload = webui_data.history_payload(state_store=self.state_store, days=30, now=NOW)
        self.assertEqual(len(payload["points"]), 1)

    def test_days_is_clamped(self) -> None:
        payload = webui_data.history_payload(state_store=self.state_store, days=-5, now=NOW)
        self.assertEqual(payload["days"], 30)
        payload = webui_data.history_payload(state_store=self.state_store, days=99999, now=NOW)
        self.assertEqual(payload["days"], 365)

    def test_empty_file(self) -> None:
        payload = webui_data.history_payload(state_store=self.state_store, days=30, now=NOW)
        self.assertEqual(payload["points"], [])
        self.assertFalse(payload["truncated"])


class TestProactivePayload(WebuiDataCase):
    def test_log_is_reversed_and_total_counts_all(self) -> None:
        lines = [
            {"ts": (NOW - timedelta(days=2)).isoformat(), "umo": "u1", "slot": "greeting", "fragment": "最早"},
            {"ts": (NOW - timedelta(days=1)).isoformat(), "umo": "u2", "slot": "night", "fragment": "中间"},
            {"ts": NOW.isoformat(), "umo": "u1", "slot": "idle", "fragment": "最新"},
        ]
        storage.atomic_write_text(
            self.layout.proactive_log,
            "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        )
        payload = webui_data.proactive_payload(proactive_store=self.proactive_store, now=NOW)
        for key in ("ok", "sessions", "log", "log_total"):
            self.assertIn(key, payload)
        self.assertEqual(payload["log_total"], 3)
        self.assertEqual([item["fragment"] for item in payload["log"]], ["最新", "中间", "最早"])  # 倒序
        for item in payload["log"]:
            for key in ("ts", "umo", "slot", "fragment"):
                self.assertIn(key, item)

    def test_sessions_shape_and_today_count(self) -> None:
        today = NOW.date().isoformat()
        self.write_json(self.layout.proactive, {"sessions": {
            "platform:qq:private:1": {"today_date": today, "today_count": 3, "last_slot": "greeting",
                                      "last_sent_at": NOW.isoformat()},
        }})
        payload = webui_data.proactive_payload(proactive_store=self.proactive_store, now=NOW)
        item = payload["sessions"][0]
        for key in ("umo", "today_count", "last_slot", "last_sent_at"):
            self.assertIn(key, item)
        self.assertEqual(item["today_count"], 3)
        self.assertEqual(item["last_slot"], "greeting")

    def test_missing_log_file_is_empty(self) -> None:
        payload = webui_data.proactive_payload(proactive_store=self.proactive_store, now=NOW)
        self.assertEqual(payload["log"], [])
        self.assertEqual(payload["log_total"], 0)
        self.assertEqual(payload["sessions"], [])


class TestFrozenContract(WebuiDataCase):
    def test_function_names(self) -> None:
        for name in ("status_payload", "diary_list_payload", "diary_content_payload",
                     "notebook_payload", "affinity_payload", "history_payload", "proactive_payload",
                     "complete_note", "forget_note"):
            self.assertTrue(callable(getattr(webui_data, name, None)), f"缺函数：{name}")

    def test_payload_params_are_keyword_only(self) -> None:
        for name in ("status_payload", "diary_list_payload", "diary_content_payload",
                     "notebook_payload", "affinity_payload", "history_payload", "proactive_payload"):
            signature = inspect.signature(getattr(webui_data, name))
            for param_name, param in signature.parameters.items():
                self.assertEqual(
                    param.kind, inspect.Parameter.KEYWORD_ONLY,
                    f"{name} 的 {param_name} 必须是关键字参数（§2.2）",
                )

    def test_write_signatures_frozen(self) -> None:
        complete = inspect.signature(webui_data.complete_note)
        self.assertEqual(
            [p.name for p in complete.parameters.values() if p.kind is not inspect.Parameter.KEYWORD_ONLY],
            ["notebook_store", "note_id"],
        )
        forget = inspect.signature(webui_data.forget_note)
        self.assertEqual(
            [p.name for p in forget.parameters.values() if p.kind is not inspect.Parameter.KEYWORD_ONLY],
            ["notebook_store", "note_id"],
        )
        self.assertTrue(inspect.iscoroutinefunction(webui_data.complete_note))
        self.assertTrue(inspect.iscoroutinefunction(webui_data.forget_note))

    def test_core_has_no_astrbot_import(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "core" / "webui_data.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                self.assertNotIn("astrbot", stripped, f"core/ 里不许 import astrbot：{stripped}")
        for target in Path(__file__).resolve().parents[1].joinpath("core").rglob("*.py"):
            text = target.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    self.assertNotIn(
                        "astrbot", stripped, f"core/ 里不许 import astrbot：{target.name} → {stripped}"
                    )


class TestHandlerLayer(WebuiDataCase):
    """handler 薄壳的离线驱动：证明"业务判断不住在 handlers 里"且**不 500**。"""

    def setUp(self) -> None:
        super().setUp()
        from web_api import handlers, routes

        self.handlers_mod = handlers
        self.routes = routes
        self.deps = SimpleNamespace(
            settings=self.settings,
            layout=self.layout,
            diary=SimpleNamespace(store=self.diary_store),
            notebook=SimpleNamespace(store=self.notebook_store),
            state=SimpleNamespace(store=self.state_store),
            affinity=SimpleNamespace(store=self.affinity_store),
            proactive=SimpleNamespace(store=self.proactive_store),
        )

    def call(self, name: str, query=None, body=None):
        fake = SimpleNamespace(
            query=dict(query or {}),
            username="admin",
            json=lambda default=None: _async_value(body if body is not None else default),
        )
        original = self.handlers_mod.request
        self.handlers_mod.request = fake
        try:
            handler = self.handlers_mod.build_handlers(self.deps)[name]
            return self.run_async(self.handlers_mod.logged_handler(name, handler)())
        finally:
            self.handlers_mod.request = original

    def data_of(self, response):
        self.assertEqual(response["_stub"], "json_response")
        self.assertEqual(response["status"], 200)
        return response["data"]

    def test_route_table_matches_handlers(self) -> None:
        available = set(self.handlers_mod.build_handlers(self.deps))
        for endpoint, handler_name, methods, description in self.routes.ROUTES:
            self.assertIn(handler_name, available, f"{endpoint} 指向的 handler 没注册")
            self.assertFalse(endpoint.startswith("/"), f"endpoint 不能带前导斜杠：{endpoint}")
            self.assertTrue(methods, f"{endpoint} 没声明方法")
            self.assertTrue(description, f"{endpoint} 没有描述")
        self.assertEqual(len(self.routes.ROUTES), len(self.routes.ENDPOINTS))

    def test_status_passthrough(self) -> None:
        data = self.data_of(self.call("status", query={"who": "u_1001"}))
        for key in ("version", "now", "who", "subsystems", "mood", "rhythm", "files", "today", "log_total"):
            self.assertIn(key, data)

    def test_query_params_are_parsed_and_clamped(self) -> None:
        data = self.data_of(self.call("history", query={"days": "99999"}))
        self.assertEqual(data["days"], 365)  # 夹到上限，不抛错
        data = self.data_of(self.call("history", query={"days": "不是数字"}))
        self.assertEqual(data["days"], 30)  # 坏参数回落默认
        data = self.data_of(self.call("diary_content", query={"book": "normal", "tail": "3"}))
        self.assertEqual(data["book"], "normal")

    def test_write_happy_path_and_idempotency(self) -> None:
        self.write_json(self.layout.notebook, {"promises": [
            {"id": "p1", "about": "u_1001", "text": "一起去看展", "done_at": ""},
        ]})
        first = self.data_of(self.call("notebook_complete", body={"id": "p1"}))
        self.assertEqual(first["ok"], True)
        second = self.data_of(self.call("notebook_complete", body={"id": "p1"}))
        self.assertEqual(second["ok"], False)  # 幂等：第二次不报错

        gone = self.data_of(self.call("notebook_delete", body={"id": "p1"}))
        self.assertEqual(gone["ok"], True)
        again = self.data_of(self.call("notebook_delete", body={"id": "p1"}))
        self.assertEqual(again["ok"], False)
        self.assertTrue(str(again.get("error") or ""))

    def test_missing_id_is_400_not_500(self) -> None:
        response = self.call("notebook_complete", body={})
        self.assertEqual(response["_stub"], "error_response")
        self.assertEqual(response["status"], 400)

    def test_broken_backend_gives_structured_error_not_exception(self) -> None:
        class Exploding:
            def read(self):
                raise RuntimeError("磁盘炸了")

            def read_mood(self):
                raise RuntimeError("磁盘炸了")

        original = self.deps.state.store
        self.deps.state = SimpleNamespace(store=Exploding())
        try:
            response = self.call("status")
        finally:
            self.deps.state = original
        self.assertEqual(response["_stub"], "error_response")
        self.assertEqual(response["status"], 500)
        self.assertIn("日志", str(response["message"]))

    def test_register_all_is_silent_without_host_support(self) -> None:
        self.assertEqual(self.routes.register_all(SimpleNamespace(), "astrbot_plugin_denia_diary", self.deps), 0)

    def test_register_all_registers_every_route(self) -> None:
        seen: list[str] = []
        context = SimpleNamespace(register_web_api=lambda path, handler, methods, desc: seen.append(path))
        count = self.routes.register_all(context, "astrbot_plugin_denia_diary", self.deps)
        self.assertEqual(count, len(self.routes.ROUTES))
        self.assertEqual(len(seen), len(self.routes.ROUTES))
        for endpoint, handler_name, methods, description in self.routes.ROUTES:
            self.assertIn(f"/astrbot_plugin_denia_diary/{endpoint}", seen)
            self.assertIn(handler_name, self.handlers_mod.build_handlers(self.deps))
        for path in seen:
            self.assertTrue(path.startswith("/astrbot_plugin_denia_diary/"))

    def test_register_all_registers_get_and_post_methods(self) -> None:
        captured: list[tuple[str, list[str]]] = []
        context = SimpleNamespace(
            register_web_api=lambda path, handler, methods, desc: captured.append((path, list(methods)))
        )
        self.routes.register_all(context, "astrbot_plugin_denia_diary", self.deps)
        by_path = dict(captured)
        self.assertEqual(by_path["/astrbot_plugin_denia_diary/status"], ["GET"])
        self.assertEqual(by_path["/astrbot_plugin_denia_diary/notebook/complete"], ["POST"])
        self.assertEqual(by_path["/astrbot_plugin_denia_diary/notebook/delete"], ["POST"])


async def _async_value(value):
    return value


if __name__ == "__main__":
    unittest.main()
