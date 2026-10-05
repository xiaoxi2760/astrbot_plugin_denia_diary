"""``astrbot.api.web`` 的**唯一**导入点。

官方 §0#6：handler 里用 ``from astrbot.api.web import request, json_response,
error_response``。这里做一层降级：astrbot 不可用（离线单测、打包自检）时给出
同签名的桩，让 ``web_api`` 的其余部分照常 import——适配层的存在不该让内核
单测必须先起一个 AstrBot。

⚠️ ``request`` **只在 handler 执行期间有效**（官方明说），别把它存起来。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于运行环境有没有 astrbot
    from astrbot.api.web import error_response, json_response, request

    ASTRBOT_AVAILABLE = True
except Exception:  # noqa: BLE001 - 离线环境：降级而不是让整个包 import 失败
    request = None  # type: ignore[assignment]
    ASTRBOT_AVAILABLE = False

    def json_response(data: Any, status_code: int = 200) -> Any:  # type: ignore[misc]
        """桩：形状够离线单测断言，不是真的 HTTP 响应。"""
        return {"_stub": "json_response", "status": int(status_code), "data": data}

    def error_response(message: str = "", status_code: int = 400, **kwargs: Any) -> Any:  # type: ignore[misc]
        """桩：同上。"""
        return {
            "_stub": "error_response",
            "status": int(status_code),
            "message": str(message),
            "extra": dict(kwargs),
        }


__all__ = ["ASTRBOT_AVAILABLE", "error_response", "json_response", "request"]
