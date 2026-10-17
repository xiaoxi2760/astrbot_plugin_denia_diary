"""薄壳 handler：解析 request → 调 ``core.webui_data`` 的纯函数 → json_response。

**业务判断一律不住在这里**（任务书 §2.2）：这个模块只做三件事——

1. 从 query / body 里取参数，夹到合理范围；
2. 调 core 的纯函数或写函数；
3. 把结果包成 JSON，异常走 ``error_response`` 而不是 500。

每个 handler 都过一层 ``logged_handler`` 统一包装（照 ``meme_manager\\web_api.py``
的写法）：把异常结构化成 ``{ok: false, error: "..."}``、顺带记 ``request.username``
做审计。WebUI 的排障只能看日志与浏览器控制台，统一包装能把两者对上。

**错误约定（§2.1 要求"二选一写死"）**：业务结果一律 **HTTP 200 + ``{ok, error}``**
（完成/删除的 id 不存在、已重复完成都属此类，契约里就是这么写的）；只有**请求
形状就不对**（缺 ``id``、body 不是对象）才用 ``error_response`` + 400。绝不 500。
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

try:  # 包内上下文（AstrBot 加载插件 / 测试用 plugin_under_test 别名加载）
    from ..core import webui_data
    from ..core import webui_portrait
    from ..core import webui_settings
    from ..core._log import logger
    from ..core.diary import card as card_mod
    from ..core.diary import card_cache
    from ..core.diary import format as fmt
except ImportError:  # 顶层上下文（离线单测直接把插件目录放进 sys.path）
    from core import webui_data  # type: ignore[no-redef]
    from core import webui_portrait  # type: ignore[no-redef]
    from core import webui_settings  # type: ignore[no-redef]
    from core._log import logger  # type: ignore[no-redef]
    from core.diary import card as card_mod  # type: ignore[no-redef]
    from core.diary import card_cache  # type: ignore[no-redef]
    from core.diary import format as fmt  # type: ignore[no-redef]

from ._web import error_response, json_response, request

MAX_TAIL = 200
"""单次最多回多少条日记正文（防止一口气把整本塞进 iframe）。"""

MAX_HISTORY_DAYS = webui_data.HISTORY_DAYS_MAX
MAX_AFFINITY_LIMIT = 200
MAX_LOG_LIMIT = 500


def build_handlers(deps: Any) -> dict[str, Callable[..., Any]]:
    """按 ``routes.ROUTES`` 的 handler 名产出闭包。``deps`` 就是插件实例。"""
    return {
        "status": _make_status(deps),
        "diary_list": _make_diary_list(deps),
        "diary_comments": _make_comments(deps),
        "diary_comment_save": _make_comments(deps, "save"),
        "diary_comment_publish": _make_comments(deps, "publish"),
        "diary_comment_withdraw": _make_comments(deps, "withdraw"),
        "diary_comment_delete": _make_comments(deps, "delete"),
        "diary_content": _make_diary_content(deps),
        "diary_rewrite": _make_diary_rewrite(deps),
        "diary_delete": _make_diary_delete(deps),
        "diary_trash": _make_diary_trash(deps),
        "diary_restore": _make_diary_restore(deps),
        "diary_card": _make_diary_card(deps),
        "diary_card_image": _make_diary_card_image(deps),
        "notebook": _make_notebook(deps),
        "notebook_complete": _make_notebook_complete(deps),
        "notebook_delete": _make_notebook_delete(deps),
        "affinity": _make_affinity(deps),
        "history": _make_history(deps),
        "proactive": _make_proactive(deps),
        "settings_get": _make_settings_get(deps),
        "settings_post": _make_settings_post(deps),
        "settings_reset": _make_settings_reset(deps),
        "portrait_get": _make_portrait_get(deps),
        "portrait_upload": _make_portrait_upload(deps),
        "portrait_select": _make_portrait_select(deps),
        "portrait_delete": _make_portrait_delete(deps),
    }


# ---- 读 ----------------------------------------------------------------------


def _make_status(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        who = _query_str(request, "who", "")
        return json_response(
            webui_data.status_payload(
                settings=deps.settings,
                layout=deps.layout,
                state_store=deps.state.store,
                proactive_store=deps.proactive.store,
                version=_plugin_version(deps),
                now=None,
                who=who,
            )
        )

    return handler


def _make_comments(deps: Any, action: str = "") -> Callable[..., Any]:
    async def handler() -> Any:
        try:
            if not action:
                book = _query_str(request, "book", fmt.NORMAL)
                seg_id = _query_str(request, "seg_id", "")
                if book not in fmt.BOOKS or not seg_id:
                    return error_response("缺少有效的 book / seg_id", 400)
                return json_response({"ok": True, "items": deps.comments.list_for(book, seg_id, settings=deps.settings)})
            body = await _read_body(request)
            if body is None:
                return error_response("请求体必须是 JSON 对象", 400)
            if action == "save":
                book, seg_id, text = body.get("book"), body.get("seg_id"), body.get("text")
                if not isinstance(book, str) or book not in fmt.BOOKS or not isinstance(seg_id, str) or not seg_id.strip() or not isinstance(text, str):
                    return error_response("缺少有效的 book / seg_id / text", 400)
                result = await deps.comments.save_draft(book, seg_id.strip(), text, deps._now())
            else:
                comment_id = body.get("id")
                if not isinstance(comment_id, str) or not comment_id.strip():
                    return error_response("缺少参数 id", 400)
                if action == "publish":
                    result = await deps.comments.publish(comment_id.strip(), deps._now(), settings=deps.settings)
                else:
                    result = await getattr(deps.comments, action)(comment_id.strip(), deps._now())
            return json_response(result)
        except (ValueError, OSError) as error:
            return json_response({"ok": False, "error": str(error)})
    return handler


def _make_diary_list(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        return json_response(
            webui_data.diary_list_payload(
                diary_store=deps.diary.store, person=_query_str(request, "who", "")
            )
        )

    return handler


def _make_diary_content(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        book = _query_str(request, "book", fmt.NORMAL)
        date = _query_str(request, "date", "")
        tail = _query_int(request, "tail", 20, low=0, high=MAX_TAIL)
        # 传 settings：正文要带上每段的 seg_id 与 editable（第 15 步），
        # 这两样都要 settings 里的时区与「面板豁免窗」开关才算得出来。
        # person：顶栏「对谁」，日历 / 正文 / 卡片三处用同一个（第 17 步）。
        # now：editable 是时间窗判定，必须吃注入时钟（与其他 handler 同口径）——
        # 裸 datetime.now() 会让种子测试在窗沿日期上半夜里集体变红（2026-10-11 实证）。
        return json_response(
            webui_data.diary_content_payload(
                diary_store=deps.diary.store,
                book=book,
                date=date,
                tail=tail,
                settings=deps.settings,
                person=_query_str(request, "who", ""),
                now=deps._now(),
            )
        )

    return handler


# ---- 日记改 / 删 / 回收站（第 15 步）--------------------------------------------
#
# 依旧薄壳：**能不能改**（时间窗 + 面板豁免开关）、备份、原子写全在 core 里。
# 这里只把 body 的 ``book`` / ``seg_id`` / ``text`` 取出来交给它。
#
# 错误约定照旧：**只有形状不对**（body 不是对象、缺 seg_id / id）才 400；
# "这段太旧了"、"找不到这一段" 都是**业务结果**，走 200 + ``{ok: false, error}`` ——
# 它们是主人会正常撞上的状态，不是接口写错了。


def _diary_entry_body(deps: Any, *, endpoint: str, action: Any, need_text: bool = False) -> Any:
    """``rewrite`` / ``delete`` 共用的壳：取 body → 校验形状 → 调 core → 200 / 400。

    ``need_text`` 同时决定**传不传** ``text``：``delete_diary_entry`` 压根没这个参数，
    无条件塞进去就是 ``TypeError`` → 500 → iframe 里只剩一句"服务端处理失败"（真机踩过的
    那种坑，静态契约测试抓不到）。所以"要不要这个键"由一处说了算。
    """

    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400, endpoint=endpoint)
        seg_id = str(body.get("seg_id") or "").strip()
        if not seg_id:
            return error_response("缺少参数 seg_id", 400, endpoint=endpoint)
        book = str(body.get("book") or fmt.NORMAL).strip() or fmt.NORMAL
        kwargs: dict[str, Any] = {
            "diary_store": deps.diary.store,
            "settings": deps.settings,
            "book": book,
            "seg_id": seg_id,
            "now": deps._now(),
        }
        if need_text:
            text = str(body.get("text") or "")
            if not text.strip():
                return error_response("缺少参数 text（要改的正文）", 400, endpoint=endpoint)
            kwargs["text"] = text
        return json_response(await action(**kwargs))

    return handler


def _make_diary_rewrite(deps: Any) -> Callable[..., Any]:
    return _diary_entry_body(
        deps,
        endpoint="diary/rewrite",
        action=webui_data.rewrite_diary_entry,
        need_text=True,
    )


def _make_diary_delete(deps: Any) -> Callable[..., Any]:
    return _diary_entry_body(
        deps, endpoint="diary/delete", action=webui_data.delete_diary_entry
    )


def _make_diary_trash(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        return json_response(
            webui_data.diary_trash_payload(
                diary_store=deps.diary.store, settings=deps.settings
            )
        )

    return handler


def _make_diary_restore(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400, endpoint="diary/restore")
        trash_id = str(body.get("id") or "").strip()
        if not trash_id:
            return error_response("缺少参数 id", 400, endpoint="diary/restore")
        result = await webui_data.restore_diary_entry(
            diary_store=deps.diary.store,
            settings=deps.settings,
            trash_id=trash_id,
            now=deps._now(),
        )
        return json_response(result)

    return handler


def _make_diary_card(deps: Any) -> Callable[..., Any]:
    """日记本预览的**渲染好的 HTML** —— 面板预览用（第 16 步）。

    和 ``/看日记`` 出图走同一份 ``core.diary.card.render_card``，所以面板里看到的
    就是真卡片。模板缺失 / 用了不支持的语法属于**服务端问题**（用户改不动模板），
    用 500 让 ``logged_handler`` 记日志，而不是给一张空白的"预览"骗人。
    """

    async def handler() -> Any:
        book = _query_str(request, "book", fmt.NORMAL)
        date = _query_str(request, "date", "")
        style = _query_str(request, "style", "")
        try:
            payload = webui_data.diary_card_payload(
                diary_store=deps.diary.store,
                settings=deps.settings,
                proactive_store=deps.proactive.store,
                book=book,
                date=date,
                style=style,
                person=_query_str(request, "who", ""),
                now=deps._now(),
            )
        except card_mod.TemplateError as error:
            logger.warning("[webui] 日记本模板有问题：%s", error)
            return error_response(f"日记本模板渲染失败：{error}", 500, endpoint="diary/card")
        return json_response(payload)

    return handler


def _make_diary_card_image(deps: Any) -> Callable[..., Any]:
    """**导出为图片**：把某一页日记本交给宿主转成 PNG（第 19 步）。

    为什么不干脆在前端拿 iframe 自己转：浏览器没有"把 DOM 画成 PNG"的原生 API，
    要么引 html2canvas 这类第三方库（插件是零依赖），要么就是截图式的近似效果——
    那就等于"面板里这张和 QQ 上那张不是同一个东西"。这里老老实实走**服务端出图**：
    复用 ``diary/card`` 的那份 HTML，再交给 ``deps.render_card_html``，和 ``/看日记``
    **完全同一条链路、同一个缓存**，所以导出的图和聊天里收到的一模一样。

    代价是它要一条能出图的通道（``pretty`` 档那条）。连不上就**明确报错**，
    不悄悄退回一张"长得差不多"的图糊弄用户。
    """

    async def handler() -> Any:
        book = _query_str(request, "book", fmt.NORMAL)
        date = _query_str(request, "date", "")
        style = _query_str(request, "style", "")
        try:
            payload = webui_data.diary_card_payload(
                diary_store=deps.diary.store,
                settings=deps.settings,
                proactive_store=deps.proactive.store,
                book=book,
                date=date,
                style=style,
                person=_query_str(request, "who", ""),
                now=deps._now(),
            )
        except card_mod.TemplateError as error:
            logger.warning("[webui] 导出图片时模板有问题：%s", error)
            return error_response(f"日记本模板渲染失败：{error}", 500, endpoint="diary/card-image")

        if payload.get("empty"):
            return json_response({"ok": True, "empty": True, "book": payload.get("book", "")})

        # payload["html"] 已经是渲好的成品，当成"模板"再传一次：里面没有 {{ }} / {% %} 了，
        # 空 ctx 渲染出来就是它本身，省得在这边重建一遍 card_context。
        html = str(payload.get("html") or "")
        if not html.strip():
            return error_response("这一页没有内容，导不出图", 400, endpoint="diary/card-image")

        try:
            # 第 20 步起与 /看日记 走同一条出图链路（缓存 → 引擎降级梯），见
            # ``main.render_card_html``。ctx 由 ``diary_card_payload`` 一并带出来——
            # **PIL 引擎画的是 ctx 不是 HTML**，没有它这条链路就落不到 pil 上。
            ctx = payload.get("ctx")
            key = card_cache.key_for_html(html, salt=str(style or ""))
            path = await deps.render_card_html(html, dict(ctx) if isinstance(ctx, Mapping) else {}, key)
            if path is None:
                raise RuntimeError("三个引擎都没出成图")
            raw = Path(str(path)).read_bytes()
        except Exception as error:  # noqa: BLE001 - 出图失败 / 文件读不到都算导出失败
            logger.warning("[webui] 日记本导出图片失败：%s", error)
            return error_response(
                f"导出失败：{error}。"
                "聊天里 /看日记 能出图就说明链路是通的，多半是这次渲染超时了，稍后再试。",
                500,
                endpoint="diary/card-image",
            )

        name = f"{payload.get('book_display') or payload.get('book') or '日记'}-{payload.get('date') or ''}.png"
        return json_response(
            {
                "ok": True,
                "empty": False,
                "book": payload.get("book"),
                "book_display": payload.get("book_display"),
                "date": payload.get("date"),
                "style": payload.get("style"),
                "filename": name,
                "mime": "image/png",
                "bytes": len(raw),
                "data_url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
            }
        )

    return handler


def _make_notebook(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        who = _query_str(request, "who", "")
        return json_response(
            webui_data.notebook_payload(
                settings=deps.settings,
                notebook_store=deps.notebook.store,
                who=who,
                proactive_store=deps.proactive.store,  # 第 8 步：小本本视图的「对谁」也要联系人昵称
            )
        )

    return handler


def _make_affinity(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        limit = _query_int(
            request, "limit", webui_data.DEFAULT_AFFINITY_LIMIT, low=1, high=MAX_AFFINITY_LIMIT
        )
        return json_response(
            webui_data.affinity_payload(
                settings=deps.settings,
                layout=deps.layout,
                affinity_store=deps.affinity.store,
                limit=limit,
            )
        )

    return handler


def _make_history(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        days = _query_int(
            request, "days", webui_data.DEFAULT_HISTORY_DAYS, low=1, high=MAX_HISTORY_DAYS
        )
        # ⚠️ now **必须传**（第 11 步独立审查订正）：不传时 core 回落 `datetime.now()`
        # 得到 **naive** 系统本地时间，而历史行里的 ts 是带偏移的 **aware**，
        # `stamp < cutoff` 直接 TypeError —— 这条路由只要有情绪历史就 500。
        return json_response(
            webui_data.history_payload(
                state_store=deps.state.store, days=days, now=deps._now()
            )
        )

    return handler


def _make_proactive(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        limit = _query_int(request, "limit", 0, low=0, high=MAX_LOG_LIMIT)
        # ⚠️ 同上：不传 now 则「今日」按**系统本地时区**算，主机在 UTC 时
        # 会与写入侧（settings.zone()）的 today_date 对不上，每日 00:00–08:00 恒显示 0。
        return json_response(
            webui_data.proactive_payload(
                proactive_store=deps.proactive.store, limit=limit, now=deps._now()
            )
        )

    return handler


# ---- 写（二期）---------------------------------------------------------------


def _make_notebook_complete(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400)
        note_id = str(body.get("id") or "").strip()
        if not note_id:
            return error_response("缺少参数 id", 400)
        # 传 now：落盘的时间戳与项目里其它时间戳一样带时区偏移
        result = await webui_data.complete_note(deps.notebook.store, note_id, now=deps._now())
        return json_response(result)

    return handler


def _make_notebook_delete(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400)
        note_id = str(body.get("id") or "").strip()
        if not note_id:
            return error_response("缺少参数 id", 400)
        # 传 deleted_at：回收站里的时间戳同样带时区偏移（forget_note 只收字符串）
        result = await webui_data.forget_note(
            deps.notebook.store, note_id, deleted_at=deps._now().isoformat(timespec="seconds")
        )
        return json_response(result)

    return handler


# ---- 设置（第 5.2 步）----------------------------------------------------------
#
# 这一节也只是薄壳：**字段表怎么展、什么值算合法，全在 core/webui_settings.py**；
# **落盘 / 备份 / 热生效全在 main.py 的 `apply_settings` 适配器方法**。
# 免得"配置正确性"有两份真相。


def _make_settings_get(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        schema = _plugin_schema(deps)
        groups, fields = webui_settings.describe_schema(schema)
        return json_response(
            {
                "ok": True,
                "sections": webui_settings.sections_payload(schema),
                "groups": groups,
                "fields": fields,
                "values": webui_settings.values_from_settings(deps.settings, schema),
                "defaults": webui_settings.defaults_from_schema(schema),
                "warnings": list(deps.settings.warnings),
                "editable_paths": webui_settings.editable_paths(fields),
                "notices": [webui_settings.READ_ONLY_ENABLED_NOTE],
                "problems": webui_settings.verify_schema_alignment(schema),
            }
        )

    return handler


def _make_settings_post(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400, endpoint="settings")
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            return error_response("缺少参数 changes（点分路径 → 新值）", 400, endpoint="settings")

        schema = _plugin_schema(deps)
        _, fields = webui_settings.describe_schema(schema)

        # 逐项 coerce：**收集全部**错误（每个坏字段都能在自己位置看到原因），整单拒绝
        clean, errors = webui_settings.review_changes(fields, changes)
        if errors:
            first = errors[0]["error"]
            return error_response(
                f"{first}（共 {len(errors)} 项没通过检查，什么都没改）",
                400,
                endpoint="settings",
                errors=errors,
            )

        base = _raw_config(deps)
        updated, problem, applied = webui_settings.plan_changes(base, clean, fields)
        if updated is None:  # 防御：review 之后不该发生（结构异常仍兜住）
            return error_response(problem, 400, endpoint="settings")

        # 唯一的校验准绳：load_settings 多报 warning 就整单拒绝，一个字节都不写。
        resolved, fresh = webui_settings.verify_settings(base, updated)
        if resolved is None:
            return error_response(
                "配置没通过校验，什么都没改："
                + "；".join(fresh)
                + "（值已按规则回落/夹紧，请改成合法值）",
                400,
                endpoint="settings",
                errors=webui_settings.warning_paths(fresh),
                problems=fresh,
            )

        result = await deps.apply_settings(updated, applied=applied)
        result.setdefault("warnings", list(resolved.warnings))
        result.setdefault("errors", [])
        result["changed"] = list(result.get("applied") or [])
        if not result.get("ok"):
            return json_response(result)  # 备份/落盘失败：业务结果 200 + {ok, error}
        if "enabled" in applied and not resolved.enabled:
            result.setdefault("notices", []).append(webui_settings.READ_ONLY_ENABLED_NOTE)
        return json_response(result)

    return handler


def _make_settings_reset(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        schema = _plugin_schema(deps)
        _, fields = webui_settings.describe_schema(schema)
        editable = set(webui_settings.editable_paths(fields))
        current = webui_settings.flatten_values(
            webui_settings.values_from_settings(deps.settings, schema)
        )
        defaults = webui_settings.defaults_by_path(fields)
        # 默认值**只从 schema 取**；只回可编辑且真的变了的项（data_dir 永不进 reset）
        changes = {
            path: default
            for path, default in defaults.items()
            if path in editable and path in current and current[path] != default
        }
        if not changes:
            return json_response(
                {
                    "ok": True,
                    "changed": [],
                    "errors": [],
                    "warnings": list(deps.settings.warnings),
                    "reloaded": False,
                    "backup": "",
                    "notices": ["所有配置本来就是默认值，什么都没改。"],
                }
            )

        base = _raw_config(deps)
        updated, problem, applied = webui_settings.plan_changes(base, changes, fields)
        if updated is None:
            return error_response(problem, 400, endpoint="settings/reset")

        resolved, fresh = webui_settings.verify_settings(base, updated)
        if resolved is None:
            return error_response(
                "恢复默认没通过校验，什么都没改：" + "；".join(fresh),
                400,
                endpoint="settings/reset",
                errors=webui_settings.warning_paths(fresh),
                problems=fresh,
            )

        result = await deps.apply_settings(updated, applied=applied)
        result.setdefault("warnings", list(resolved.warnings))
        result.setdefault("errors", [])
        result["changed"] = list(result.get("applied") or [])
        if result.get("ok"):
            result.setdefault("notices", []).append(
                f"已把 {len(result['changed'])} 项恢复为默认值（默认值只来自 _conf_schema.json）。"
            )
        return json_response(result)

    return handler


def _raw_config(deps: Any) -> dict[str, Any]:
    """插件手里的**活配置对象**（``AstrBotConfig`` 是 dict 子类，直接当 dict 用）。"""
    config = getattr(deps, "config", None)
    try:
        return dict(config) if config else {}
    except Exception:  # noqa: BLE001 - 配置对象异常不该让面板 500
        return {}


def _plugin_schema(deps: Any) -> dict[str, Any]:
    """schema = ``self.config.schema``；拿不到就读插件自己的 ``_conf_schema.json``。

    读文件是必要的兜底：离线单测的 ``deps.config`` 是普通 dict，而字段表**不能**
    在两处各写一份。
    """
    schema = getattr(getattr(deps, "config", None), "schema", None)
    if isinstance(schema, dict) and schema:
        return schema
    import json  # noqa: PLC0415 - 只在真的需要兜底时导入
    from pathlib import Path  # noqa: PLC0415

    path = Path(__file__).resolve().parent.parent / "_conf_schema.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("[webui] 读不到配置 schema：%s", error)
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---- 立绘（第 5.3 步）----------------------------------------------------------
#
# 依旧薄壳：判定（魔数 / 大小 / 数量 / 净化）与落盘纪律全在 ``core/webui_portrait.py``。
# 这里只做两件宿主相关的事——``content_length`` 预检 + ``request.files()`` 取文件，
# 以及 ``save`` 的同步 / 异步兼容垫（在 core 的 ``read_upload`` 里，鸭子类型不 import astrbot）。


def _make_portrait_get(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        portrait_id = _query_str(request, "id", "")
        try:
            result = await deps.portraits.snapshot(only_id=portrait_id or None)
        except webui_portrait.PortraitError as error:
            return error_response(str(error), 400, endpoint="portrait")
        return json_response(result)

    return handler


def _make_portrait_upload(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        # 两层保险之一：先看 content_length——超大 body 直接 400，不读进内存
        length = getattr(request, "content_length", None)
        if isinstance(length, int) and length > webui_portrait.MAX_UPLOAD_BODY_BYTES:
            return error_response("上传体积超过上限（单张 8 MB）。", 400, endpoint="portrait/upload")
        # 两层保险之二：能设框架层的 max_content_length 就设（宿主不一定有这属性）
        try:
            request.max_content_length = webui_portrait.MAX_UPLOAD_BODY_BYTES
        except (AttributeError, RuntimeError):
            pass

        try:
            files = await request.files()
        except Exception as error:  # noqa: BLE001 - multipart 解析失败不冒 500
            logger.warning("[webui] portrait/upload 解析 multipart 失败：%s", error)
            return error_response("上传内容解析失败，请重试。", 400, endpoint="portrait/upload")
        upload = files.get("file") if files else None
        if upload is None:
            return error_response("缺少上传文件字段 file。", 400, endpoint="portrait/upload")

        data = await webui_portrait.read_upload(upload, deps.portraits.base)
        try:
            result = await deps.portraits.upload(
                data,
                getattr(upload, "filename", ""),
                now=deps._now(),
            )
        except webui_portrait.PortraitError as error:
            return error_response(str(error), 400, endpoint="portrait/upload")
        return json_response(result)

    return handler


def _portrait_body_action(deps: Any, *, endpoint: str, action: Any) -> Callable[..., Any]:
    """``select`` / ``delete`` 共用的壳：body ``{"id": ...}`` → core → 200 / 400。"""

    async def handler() -> Any:
        body = await _read_body(request)
        if body is None:
            return error_response("请求体必须是 JSON 对象", 400, endpoint=endpoint)
        portrait_id = str(body.get("id") or "").strip()
        if not portrait_id:
            return error_response("缺少参数 id", 400, endpoint=endpoint)
        try:
            result = await action(deps.portraits, portrait_id)
        except webui_portrait.PortraitError as error:
            return error_response(str(error), 400, endpoint=endpoint)
        return json_response(result)

    return handler


def _make_portrait_select(deps: Any) -> Callable[..., Any]:
    return _portrait_body_action(
        deps,
        endpoint="portrait/select",
        action=lambda store, portrait_id: store.select(portrait_id),
    )


def _make_portrait_delete(deps: Any) -> Callable[..., Any]:
    return _portrait_body_action(
        deps,
        endpoint="portrait/delete",
        action=lambda store, portrait_id: store.delete(portrait_id),
    )


# ---- 统一包装 ----------------------------------------------------------------


def logged_handler(name: str, handler: Callable[..., Any]) -> Callable[..., Any]:
    """给每个 handler 套一层：结构化异常 → ``error_response``，绝不让异常冒出 500。

    顺带记 ``request.username`` 做审计（数值只给登录态看，这里只留个痕迹）。
    """

    async def wrapper() -> Any:
        try:
            return await handler()
        except Exception as error:  # noqa: BLE001 - 这就是目的：把异常吃掉
            logger.exception("[webui] %s 失败（操作者=%s）", name, _username())
            return error_response("服务端处理失败，请看插件日志", 500, endpoint=name)

    wrapper.__name__ = f"webui_{name}"
    return wrapper


# ---- 参数解析 ----------------------------------------------------------------


def _query_str(request: Any, key: str, default: str = "") -> str:
    try:
        value = request.query.get(key, default)
    except Exception:  # noqa: BLE001 - 拿不到 query 就用缺省，不让面板白屏
        return default
    if value is None:
        return default
    return str(value).strip()


def _query_int(request: Any, key: str, default: int, *, low: int, high: int) -> int:
    raw = _query_str(request, key, "")
    if not raw:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(value, high))


async def _read_body(request: Any) -> dict[str, Any] | None:
    """读 JSON body；不是对象就返回 ``None``（调用方转 400）。"""
    try:
        data = await request.json(default={})
    except Exception:  # noqa: BLE001 - 坏 JSON 不该 500
        return None
    return data if isinstance(data, dict) else None


def _username() -> str:
    try:
        return str(getattr(request, "username", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _plugin_version(deps: Any) -> str:
    """插件版本：``metadata.yaml`` 的 ``version:``（纯文本正则，不引 yaml 依赖）。"""
    cached = getattr(deps, "_webui_version", None)
    if cached is not None:
        return str(cached)
    import re  # noqa: PLC0415 - 只这里用一次
    from pathlib import Path

    version = ""
    meta = Path(__file__).resolve().parent.parent / "metadata.yaml"
    try:
        text = meta.read_text(encoding="utf-8")
        match = re.search(r"^version:\s*[\"']?([^\s\"']+)", text, re.MULTILINE)
        if match:
            version = match.group(1)
    except OSError:
        version = ""
    try:
        deps._webui_version = version
    except Exception:  # noqa: BLE001 - 只读属性类就跳过缓存
        pass
    return version


__all__ = ["build_handlers", "logged_handler"]
