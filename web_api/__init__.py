"""WebUI 的 HTTP 胶水层（第 5 步）。

**这个包是本项目里除 ``main.py`` 之外唯一 import astrbot 的地方**——``core/``
仍然零 astrbot 依赖，业务判断也仍然住在 ``core/webui_data.py`` 的纯函数里。

按任务书 §1.1 的形：

- ``routes.py``：路由表 + ``register_all(context, plugin_name, deps)``（表驱动）
- ``handlers.py``：薄壳 handler，解析 request → 调 core 纯函数 → json_response
- ``_web.py``：**唯一** import ``astrbot.api.web`` 的模块

三个模块都能在**没有 astrbot 的环境**里 import（``_web`` 的导入失败会降级成
可用的桩），所以离线路径对账脚本可以直接读路由表、离线单测可以直接调 handler。
"""

from __future__ import annotations

from .handlers import build_handlers
from .routes import ROUTES, register_all

__all__ = ["ROUTES", "build_handlers", "register_all"]
