"""全插件唯一的 logger 来源（硬性规范：不得自建 ``logging`` logger）。

规范要求 logger **只能**来自 ``from astrbot.api import logger``——只有这样日志才会
进 AstrBot WebUI 的日志面板、跟着宿主的日志级别走，而不是散在插件自己的 handler 里。
所以本插件任何模块都**不许**写 ``import logging`` / ``logging.getLogger(__name__)``。

但 ``core/`` 与 ``web_api/`` 同时还有另一条铁律：**没有 astrbot 也要能 import**
（离线单测直接把它们当普通包加载，见 ``web_api/_web.py`` 的同款降级）。两条规则
正面撞车时的**裁定（第 13 步审核整改，2026-10）**：审核规范 A（只能 ``astrbot.api``
logger）**高于**本仓约定 B（``core/`` 零 astrbot import），B 的目的——离线能 import、
离线能测——在 A 之下仍要保住。解法就是这里：**一次降级，全局复用**。

⚠️ **本文件是全仓唯一允许出现 ``astrbot`` 字样的内核模块，且那行 import 必须包在
``try/except`` 里**（只捕 ``ImportError``）。守卫按此口径放行本文件、其余 ``core/``
模块一律判红，共三处：``tools/webui_selfcheck.py`` 的 core/ 扫描、
``test/test_webui_data.py`` 的 ``test_core_has_no_astrbot_import``、
``test/test_outbound.py`` 的同名守卫。别在本文件之外再引入 astrbot import。

⚠️ 降级分支里**故意不 import logging**：规范明令禁止，空壳（``_NullLogger``）已经够用——
离线单测本来就不看日志输出，而在真机上永远走的是宿主 logger 那条路。
"""

from __future__ import annotations

from typing import Any

__all__ = ["logger"]


class _NullLogger:
    """离线替身：接口与 ``logging.Logger`` 同形（``%s`` 惰性格式化），但什么都不做。

    刻意保持 ``debug/info/warning/error/exception/critical`` 全都存在：调用点是按
    这些名字写的，缺一个就是 ``AttributeError``，而丢日志绝不该把业务带崩。
    """

    def _noop(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        return None

    debug = _noop
    info = _noop
    warning = _noop
    error = _noop
    exception = _noop
    critical = _noop
    log = _noop


try:  # pragma: no cover - 取决于运行环境有没有 astrbot
    from astrbot.api import logger  # type: ignore[assignment]
except ImportError:  # 降级而不是让整个包 import 失败（裁定见模块 docstring）
    logger = _NullLogger()  # type: ignore[assignment]