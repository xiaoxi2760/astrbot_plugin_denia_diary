"""路由表 + 表驱动注册（第 5 步，形态照 ``denia_share`` 的 ``core/webui.py``）。

两条硬约束：

1. **endpoint 不带插件名前缀**（官方 §0#4）：前端写 ``"status"``；
   Dashboard 转发到 ``/api/v1/plugins/extensions/<plugin_name>/<endpoint>``。
   反过来，**注册时必须带前缀**（§0#5）：``f"/{plugin_name}/status"``。
2. 前端 endpoint **不能以 ``/`` 开头**（§0#11，作者踩过）——``ROUTES`` 里的
   ``endpoint`` 字段就是前端该写的字面量，离线对账脚本拿它跟前端源码核对。

本模块**不 import astrbot**（``handlers`` 也不会在 import 期拉 astrbot），
所以离线路径对账脚本可以直接 import 它读路由表。
"""

from __future__ import annotations

import logging
from typing import Any

from .handlers import build_handlers, logged_handler

logger = logging.getLogger(__name__)

RouteSpec = tuple[str, str, tuple[str, ...], str]
"""``(endpoint, handler 名, 方法, 描述)``——``endpoint`` 是不带前缀、不带前导斜杠的。"""

ROUTES: tuple[RouteSpec, ...] = (
    ("status", "status", ("GET",), "总览：此刻的情绪、作息、今日主动计数与文件体检"),
    ("diary/list", "diary_list", ("GET",), "两本日记的篇数、字数与最近更新"),
    ("diary/content", "diary_content", ("GET",), "读一段日记正文（只读）"),
    ("notebook", "notebook", ("GET",), "小本本：事实与约定（按人过滤）"),
    ("notebook/complete", "notebook_complete", ("POST",), "把一条约定标记为完成"),
    ("notebook/delete", "notebook_delete", ("POST",), "删一条事实或约定（先进回收站）"),
    ("affinity", "affinity", ("GET",), "熟悉度榜、档位与当前 love_peers（只显示）"),
    ("history", "history", ("GET",), "情绪曲线数据点（来自 state_history.jsonl）"),
    ("proactive", "proactive", ("GET",), "主动消息计数与发送记录"),
    ("settings", "settings_get", ("GET",), "设置页：schema 驱动的字段表、当前值与默认值"),
    ("settings", "settings_post", ("POST",), "保存设置（校验 → 备份 → 落盘 → 热生效）"),
)

ENDPOINTS: tuple[str, ...] = tuple(spec[0] for spec in ROUTES)
"""全部 endpoint 字面量（离线路径对账用）。"""


def register_all(context: Any, plugin_name: str, deps: Any) -> int:
    """把 ``ROUTES`` 逐条注册到宿主，返回注册条数。

    **兼容守卫**：AstrBot < 4.24.2 没有 ``register_web_api``（官方 §0#7），
    这时**静默跳过**——面板不可用，但插件本体照常在聊天里工作。
    """
    register = getattr(context, "register_web_api", None)
    if not callable(register):
        logger.info(
            "[%s] 宿主没有 register_web_api（AstrBot 版本偏老），WebUI 页面不注册",
            plugin_name,
        )
        return 0
    handlers = build_handlers(deps)
    prefix = f"/{plugin_name}"
    for endpoint, handler_name, methods, description in ROUTES:
        handler = handlers.get(handler_name)
        if handler is None:  # pragma: no cover - 路由表与 build_handlers 不一致时兜底
            logger.error("[%s] 路由 %s 找不到 handler %s", plugin_name, endpoint, handler_name)
            continue
        try:
            register(prefix + "/" + endpoint, logged_handler(endpoint, handler), list(methods), description)
        except Exception:  # noqa: BLE001 - 一个路由注册失败不该让插件加载失败
            logger.exception("[%s] 路由 %s 注册失败", plugin_name, endpoint)
    logger.info("[%s] WebUI 页面路由已注册：%d 条（插件页 diary）", plugin_name, len(ROUTES))
    return len(ROUTES)


__all__ = ["ENDPOINTS", "ROUTES", "RouteSpec", "register_all"]
