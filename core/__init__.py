"""陪伴系统插件内核。

**零 AstrBot 依赖**：本包只使用标准库，可在没有 AstrBot 的环境里完整离线测试。
适配器（``main.py``）负责把平台对象翻译成这里的调用，不在这里做平台判断。

当前进度：第 0 步（存储契约：``storage`` + ``settings``）。
"""

__all__ = ["settings", "storage"]
