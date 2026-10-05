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

import logging
from typing import Any, Callable

try:  # 包内上下文（AstrBot 加载插件 / 测试用 plugin_under_test 别名加载）
    from ..core import webui_data
    from ..core import webui_settings
    from ..core.diary import format as fmt
except ImportError:  # 顶层上下文（离线单测直接把插件目录放进 sys.path）
    from core import webui_data  # type: ignore[no-redef]
    from core import webui_settings  # type: ignore[no-redef]
    from core.diary import format as fmt  # type: ignore[no-redef]

from ._web import error_response, json_response, request

logger = logging.getLogger(__name__)

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
        "diary_content": _make_diary_content(deps),
        "notebook": _make_notebook(deps),
        "notebook_complete": _make_notebook_complete(deps),
        "notebook_delete": _make_notebook_delete(deps),
        "affinity": _make_affinity(deps),
        "history": _make_history(deps),
        "proactive": _make_proactive(deps),
        "settings_get": _make_settings_get(deps),
        "settings_post": _make_settings_post(deps),
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


def _make_diary_list(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        return json_response(webui_data.diary_list_payload(diary_store=deps.diary.store))

    return handler


def _make_diary_content(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        book = _query_str(request, "book", fmt.NORMAL)
        date = _query_str(request, "date", "")
        tail = _query_int(request, "tail", 20, low=0, high=MAX_TAIL)
        return json_response(
            webui_data.diary_content_payload(
                diary_store=deps.diary.store, book=book, date=date, tail=tail
            )
        )

    return handler


def _make_notebook(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        who = _query_str(request, "who", "")
        return json_response(
            webui_data.notebook_payload(
                settings=deps.settings, notebook_store=deps.notebook.store, who=who
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
        return json_response(webui_data.history_payload(state_store=deps.state.store, days=days))

    return handler


def _make_proactive(deps: Any) -> Callable[..., Any]:
    async def handler() -> Any:
        limit = _query_int(request, "limit", 0, low=0, high=MAX_LOG_LIMIT)
        return json_response(
            webui_data.proactive_payload(proactive_store=deps.proactive.store, limit=limit)
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
        result = await webui_data.complete_note(deps.notebook.store, note_id)
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
        result = await webui_data.forget_note(deps.notebook.store, note_id)
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
                "groups": groups,
                "fields": fields,
                "values": webui_settings.values_from_settings(deps.settings, schema),
                "defaults": webui_settings.defaults_from_schema(schema),
                "warnings": list(deps.settings.warnings),
                "editable_paths": webui_settings.editable_paths(fields),
                "notices": [webui_settings.READ_ONLY_ENABLED_NOTE],
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
        base = _raw_config(deps)
        updated, problem, applied = webui_settings.plan_changes(base, changes, fields)
        if updated is None:
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
                problems=fresh,
            )

        result = await deps.apply_settings(updated, applied=applied)
        result.setdefault("warnings", list(resolved.warnings))
        if not result.get("ok"):
            return json_response(result)  # 备份/落盘失败：业务结果 200 + {ok, error}
        if "enabled" in applied and not resolved.enabled:
            result.setdefault("notices", []).append(webui_settings.READ_ONLY_ENABLED_NOTE)
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
