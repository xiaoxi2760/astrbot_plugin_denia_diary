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

from typing import Any

try:  # 包内 / 顶层上下文都行（同一个 logger 模块，见 core/_log.py 的降级说明）
    from ..core._log import logger
except ImportError:  # pragma: no cover - 顶层上下文（离线单测）
    from core._log import logger  # type: ignore[no-redef]

from .handlers import build_handlers, logged_handler

RouteSpec = tuple[str, str, tuple[str, ...], str]
"""``(endpoint, handler 名, 方法, 描述)``——``endpoint`` 是不带前缀、不带前导斜杠的。"""

ROUTES: tuple[RouteSpec, ...] = (
    ("pet/chat-devices", "pet_chat_devices", ("GET",), "统一聊天 v2 设备与已有核心用户（管理鉴权）"),
    ("pet/chat-devices/issue", "pet_chat_device_issue", ("POST",), "为当前实例已有核心用户签发 v2 聊天设备绑定"),
    ("pet/chat-devices/revoke", "pet_chat_device_revoke", ("POST",), "撤销当前实例的 v2 设备凭据"),
    ("core/snapshot", "core_snapshot", ("GET",), "核心管理：预设、历史、记忆、好感度与观测值（宿主管理鉴权）"),
    ("core/action", "core_action", ("POST",), "核心管理操作（由核心服务校验并保存）"),
    ("instances", "instances", ("GET",), "管理面板可选择的 Bot 实例"),
    ("status", "status", ("GET",), "总览：情绪、作息、今日主动计数与文件体检"),
    ("life", "life", ("GET",), "今天的公开日常：计划、当前活动与进展"),
    ("life/generate", "life_generate", ("POST",), "尝试规划今天的未来活动"),
    ("life/replace", "life_replace", ("POST",), "调整未来活动，保留已经开始的安排"),
    ("life/record", "life_record", ("POST",), "确认当前或过去活动的公开结果"),
    ("diary/list", "diary_list", ("GET",), "两本日记的篇数、字数与最近更新"),
    ("diary/comments", "diary_comments", ("GET",), "查看一段日记的留言"),
    ("diary/comments/save", "diary_comment_save", ("POST",), "保存留言草稿，不通知她"),
    ("diary/comments/publish", "diary_comment_publish", ("POST",), "让她下次私聊时看到这条留言"),
    ("diary/comments/withdraw", "diary_comment_withdraw", ("POST",), "撤回尚未交付的留言"),
    ("diary/comments/delete", "diary_comment_delete", ("POST",), "归档删除留言"),
    ("diary/content", "diary_content", ("GET",), "读日记正文（含每段的 seg_id 与可否改）"),
    ("diary/rewrite", "diary_rewrite", ("POST",), "改一段日记的正文（头行不动）"),
    ("diary/delete", "diary_delete", ("POST",), "删一段日记（先进回收站）"),
    ("diary/trash", "diary_trash", ("GET",), "回收站：删掉的那些段"),
    ("diary/restore", "diary_restore", ("POST",), "从回收站还原一段（幂等）"),
    ("diary/card", "diary_card", ("GET",), "日记卡片的渲染好的 HTML（面板预览用）；?book=&date=&style="),
    ("diary/card-image", "diary_card_image", ("GET",), "导出日记本为图片（服务端出图，回 data:image/png;base64,…）；?book=&date=&style="),
    ("notebook", "notebook", ("GET",), "小本本：事实与约定（按人过滤）"),
    ("notebook/complete", "notebook_complete", ("POST",), "把一条约定标记为完成"),
    ("notebook/delete", "notebook_delete", ("POST",), "删一条（先进回收站）"),
    ("affinity", "affinity", ("GET",), "熟悉度榜、档位与当前 love_peers"),
    ("history", "history", ("GET",), "情绪曲线数据点"),
    ("proactive", "proactive", ("GET",), "主动消息计数与发送记录"),
    ("social", "social_get", ("GET",), "群社交：登记的群与参与者、待登记、话题概览与最近决策"),
    ("social/group", "social_group", ("POST",), "登记/更新/删除群（body: action=save|delete；默认不开主动发言）"),
    ("social/participant", "social_participant", ("POST",), "登记/更新/删除参与者（body: action=save|delete，真实 id，人或 bot 由主人标注）"),
    ("social/preview", "social_preview", ("POST",), "测试桩：生成候选/短聊判定预览，绝不发送真实消息"),
    ("voice/status", "voice_status", ("GET",), "语音能力与诊断：识别 / 合成 / 文件服务 / 最近事件"),
    ("pet/devices", "pet_devices", ("GET",), "桌宠设备与状态监听器（管理面板）"),
    ("pet/devices/issue", "pet_device_issue", ("POST",), "为主人签发一次显示的设备凭据"),
    ("pet/devices/revoke", "pet_device_revoke", ("POST",), "撤销桌宠设备凭据"),
    ("settings", "settings_get", ("GET",), "设置页字段表、当前值与默认值"),
    ("settings", "settings_post", ("POST",), "保存设置（校验 → 备份 → 落盘 → 热生效）"),
    ("settings/reset", "settings_reset", ("POST",), "恢复默认设置"),
    ("portrait", "portrait_get", ("GET",), "立绘：当前图（含 data_url）与列表；?id= 取单张"),
    ("portrait/upload", "portrait_upload", ("POST",), "上传立绘（multipart 字段 file，png/jpeg/webp/gif，≤8 MB / ≤20 张）"),
    ("portrait/upload-data", "portrait_upload_data", ("POST",), "按实例上传立绘（JSON data URL，净化与配额沿用立绘服务）"),
    ("portrait/select", "portrait_select", ("POST",), "切换当前立绘"),
    ("portrait/delete", "portrait_delete", ("POST",), "删除一张立绘（连文件）"),
    ("imagery", "imagery_status", ("GET",), "状态生图：开关、能力、配置、今日用量与最近一张的元信息"),
    ("imagery/image", "imagery_image", ("GET",), "最近一张生图（回 data:image/png;base64,…；无图回 ok:false）"),
    ("imagery/generate", "imagery_generate", ("POST",), "画一下现在的她（按需；受开关、每日上限与缓存约束，不自动发消息）"),
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
