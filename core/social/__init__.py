"""群社交（任务 1）：登记的群里适度参与，登记 bot 短聊有结束条件。

对外只暴露 ``Social`` 门面；判定细节在 ``chat``（短聊闸门）、``topics``（有界
素材）、``registry``（身份登记）。零 astrbot import，可离线测试。
"""

from .api import Social

__all__ = ["Social"]
