"""主动消息（第 4 步）：触发器（有什么可说）/ 闸门（该不该说）/ 出站（怎么说）。

三件套职责分离、互不知情（框架 §4.4 v0.6 定稿）：

- ``triggers``：七个触发器只产候选（说给谁、原文片段、优先级），无权决定发不发；
- ``gate``：三条硬约束 + 各级冷却 + 暗号独立四道闸，先闸门后内容；
- ``api``（``Proactive`` 门面）：巡检决策、两段式 ``last_sent_at``、出站素材组装。

调度（``cron_manager`` 的 basic/active job）与确认钩子（``on_using_llm_tool``）
全在 ``main.py``——core 不 import astrbot，闸门 / 触发器 / 优先级判定全部离线可测。
"""

from .api import Proactive
from .store import ProactiveStore
from .triggers import PRIORITY_ORDER, Candidate

__all__ = ["Proactive", "ProactiveStore", "Candidate", "PRIORITY_ORDER"]
