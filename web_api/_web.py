"""``astrbot.api.web`` 的**唯一**导入点。

官方 §0#6：handler 里用 ``from astrbot.api.web import request, json_response,
error_response``。这里做两层事：

1. **降级**：astrbot 不可用（离线单测、打包自检）时给出同签名的桩，让 ``web_api``
   的其余部分照常 import——适配层的存在不该让内核单测先起一个 AstrBot。
2. **归一**（2026-10-06 真机踩坑后加的，见下）：宿主的 ``error_response`` 签名很窄，
   本层把它包成本仓库内部统一、宽容的那一个。

⚠️ ``request`` **只在 handler 执行期间有效**（官方明说），别把它存起来。

## 为什么要有第 2 层：真机上所有错误路径都变 500

真机 4.28.1 的签名是::

    error_response(message: str, *, status_code: int = 400, data: Any = None,
                   headers: dict | None = None)

``status_code`` 是**关键字限定**，而且**不接任意 kwarg**。而本仓库 handler 里到处是
``error_response(msg, 400, endpoint="x")`` —— 真机上直接::

    TypeError: error_response() got an unexpected keyword argument 'endpoint'

它被 ``logged_handler`` 的 ``except`` 接住后**再调一次同样的错签名**，于是连兜底的 500
也一起炸穿，框架只能回 ``{"status":"error","message":"Internal server error"}``。

**离线全绿、真机全 500** 的根因是桩太宽容（``**kwargs`` 照单全收）——离线测的东西
和真机跑的**不是同一套签名**，于是"测试通过"对真机没有任何保证。

所以本层定死一个内部签名，把附加信息收进宿主信封的 ``data`` 字段::

    error_response(message, status_code=400, *, errors=None, problems=None,
                   endpoint="", headers=None)

**铁律**：调用宿主的唯一姿势是 ``message`` 位置参 + ``status_code`` / ``data`` /
``headers`` 关键字（``_build`` 里那两行）。离线桩与真机路径**共用同一段组装逻辑**
（``_envelope``），只有最后一步"交给谁发"不同——这样离线跑通的错误响应，真机上
必然同形。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于运行环境有没有 astrbot
    from astrbot.api.web import json_response, request
    from astrbot.api.web import error_response as _host_error_response

    ASTRBOT_AVAILABLE = True
except Exception:  # noqa: BLE001 - 离线环境：降级而不是让整个包 import 失败
    request = None  # type: ignore[assignment]
    ASTRBOT_AVAILABLE = False

    def json_response(data: Any, status_code: int = 200) -> Any:  # type: ignore[misc]
        """桩：形状够离线单测断言，不是真的 HTTP 响应。"""
        return {"_stub": "json_response", "status": int(status_code), "data": data}


def _envelope(
    *,
    errors: Any = None,
    problems: Any = None,
    endpoint: str = "",
) -> dict[str, Any] | None:
    """把附加信息装进宿主的 ``data`` 字段；一条都没有就回 ``None``（别发空对象）。"""
    data: dict[str, Any] = {}
    if endpoint:
        data["endpoint"] = str(endpoint)
    if errors:
        data["errors"] = list(errors)
    if problems:
        data["problems"] = list(problems)
    return data or None


if ASTRBOT_AVAILABLE:

    def _build(message: str, status_code: int, data: dict[str, Any] | None, headers: Any) -> Any:
        """**调用宿主的唯一姿势**——只许 message 位置参 + 这三个关键字。"""
        return _host_error_response(message, status_code=status_code, data=data, headers=headers)

else:

    def _build(message: str, status_code: int, data: dict[str, Any] | None, headers: Any) -> Any:
        """桩：有附加信息时 ``data`` 与 ``extra`` **就是同一个 dict**，两种读法都行。"""
        return {
            "_stub": "error_response",
            "status": int(status_code),
            "message": message,
            "data": data,
            "extra": data if data is not None else {},
        }


def error_response(
    message: str = "",
    status_code: int = 400,
    *,
    errors: Any = None,
    problems: Any = None,
    endpoint: str = "",
    headers: Any = None,
) -> Any:
    """统一的错误出口（本仓库内部签名，别再往宿主的窄签名上直接调）。

    ``errors`` / ``problems`` / ``endpoint`` 走宿主信封的 ``data`` 字段；前端读
    ``error.data.errors``（离线桩里 ``data`` 与 ``extra`` 同物）。
    """
    return _build(
        str(message),
        int(status_code),
        _envelope(errors=errors, problems=problems, endpoint=endpoint),
        headers,
    )


__all__ = ["ASTRBOT_AVAILABLE", "error_response", "json_response", "request"]
