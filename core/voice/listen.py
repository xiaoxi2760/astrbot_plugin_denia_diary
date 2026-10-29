"""被动收听（F2）：主人发语音时，**优先复用宿主的转写结果**，插件不做第二份 ASR。

上游事实（宿主 4.28.1 ``preprocess_stage``，实证见交付报告）：宿主 STT 开启且成功时，
会把消息链里的 ``Record`` **就地替换**成带转写文本的 ``Plain``，并把文本追加进
``message_str``——插件看到的已经是文字，什么都不用做。

宿主没转（STT 关了 / 没配提供方 / 转写失败）时链里会**剩下 Record**：适配层可以
拿宿主自己的 STT provider 兜底转一次（入口、去重、配额都在这份决策里，仍用宿主的
提供方配置，不自建 ASR）。去重的两层保证：

1. **链内**：转写成功后 Record 已被替换成 Plain——同一事件重放时看到的不是 Record，
   不会再转；
2. **账本**：``ListenState`` 按 (事件 id, 音频指纹) 记"最近转过"，LRU + TTL，
   重启丢失无害（最坏情况 = 多转一次，不是重复回复）。

红线：这里全是纯判定，零 IO、零 astrbot——链上的组件是**鸭子类型**（有 ``type`` /
``file`` / ``url`` / ``path`` 属性就算数），离线测试用同形假对象。
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote, urlparse

MAX_KEYS_DEFAULT = 256
"""去重账本的容量上限：一页语音消息的量级，超了丢最老的（LRU）。"""

TTL_SECONDS_DEFAULT = 600.0
"""账本条目的存活时间（秒）：比任何一次重放窗口都长，比"内存当账本"的隐患短。"""

MAX_RECORDS_PER_EVENT = 4
"""单条消息最多兜底转写几段 Record：正常一条语音就是一段，多段是异常（防刷）。"""


def file_uri_to_path(uri: str) -> str | None:
    """``file:///home/u/a.wav`` → ``/home/u/a.wav``（含 %20 解码）；不是 file URI 返回 None。

    宿主 ``Record.fromFileSystem`` 把本地路径包成 file URI 存进 ``file`` 字段——
    取指纹时先解回来。
    """
    text = str(uri or "").strip()
    if not text.lower().startswith("file:"):
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme.lower() != "file":
        return None
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in ("localhost", ""):
        path = f"//{parsed.netloc}{path}"
    # Windows 盘符形态：file:///C:/... → /C:/...（urlparse 把盘符算进了 path）——剥掉前导斜杠
    if re.fullmatch(r"/[A-Za-z]:/.*", path):
        path = path[1:]
    return path or None


def record_ref(component: Any) -> str | None:
    """一段语音的**身份指纹**：本地路径优先，其次 file URI、url，最后裸 ``file``。

    裸 ``file`` 在部分适配器里只是模糊标识；宿主提供的 ``url`` 通常更有区分度。
    这里只取字段作为去重指纹，不解析远程地址或读取网络内容。
    """
    if component is None:
        return None

    def usable_ref(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        decoded = file_uri_to_path(text)
        if decoded:
            return decoded
        if text.lower().startswith("file:"):
            return None  # 无效 file URI：尝试其他字段
        return text

    path_ref = usable_ref(getattr(component, "path", None))
    if path_ref:
        return path_ref

    file_value = str(getattr(component, "file", None) or "").strip()
    file_uri_ref = file_uri_to_path(file_value)
    if file_uri_ref:
        return file_uri_ref
    bare_file_ref = None if file_value.lower().startswith("file:") else file_value or None

    url_ref = usable_ref(getattr(component, "url", None))
    if url_ref:
        return url_ref
    return bare_file_ref


def is_record(component: Any) -> bool:
    """鸭子类型判定：``type`` 属性是 ``Record``（str 枚举）或类名就叫 ``Record``。"""
    if component is None or isinstance(component, (str, bytes, dict)):
        return False
    kind = getattr(component, "type", None)
    if kind is not None and str(getattr(kind, "value", kind)).lower() == "record":
        return True
    return type(component).__name__ == "Record"


def pending_records(chain: Any) -> list[tuple[int, str]]:
    """消息链里**还没被转写**的语音段：``[(下标, 指纹), ...]``（按出现顺序，截 MAX 条）。

    宿主转写成功会把 Record 换成 Plain——所以"链里还有 Record"本身就等于
    "宿主这一段没转"，这正是兜底的判定依据。坏输入（非列表 / 空链）返回空表。
    """
    if not isinstance(chain, (list, tuple)):
        return []
    out: list[tuple[int, str]] = []
    for index, component in enumerate(chain):
        if not is_record(component):
            continue
        ref = record_ref(component) or f"record#{index}"
        out.append((index, ref))
        if len(out) >= MAX_RECORDS_PER_EVENT:
            break
    return out


class ListenState:
    """兜底转写的去重账本（进程内）：同一个 (事件, 音频) 只兜底转一次。

    ``first_time(key)`` 是唯一的读改写口：第一次见到返回 True 并记账，之后再问
    一律 False。TTL 过期与超容量淘汰最老的（按插入序近似 LRU）。
    """

    def __init__(
        self, *, max_keys: int = MAX_KEYS_DEFAULT, ttl_seconds: float = TTL_SECONDS_DEFAULT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_keys = max(1, int(max_keys))
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._seen: dict[str, float] = {}

    def first_time(self, key: str) -> bool:
        now = self._clock()
        cutoff = now - self._ttl
        key_text = str(key or "").strip()
        # 命中判定必须在淘汰**之前**：先淘汰会把刚记的键也挤出去（假"第一次"）。
        if key_text and key_text in self._seen and self._seen[key_text] >= cutoff:
            return False
        if len(self._seen) >= self._max_keys or any(ts < cutoff for ts in self._seen.values()):
            alive = {k: ts for k, ts in self._seen.items() if ts >= cutoff}
            while len(alive) >= self._max_keys:
                oldest = min(alive, key=alive.get)  # type: ignore[arg-type]
                alive.pop(oldest, None)
            self._seen = alive
        if not key_text:
            return True  # 没有指纹可记：按"第一次"处理（转不转由调用方定）
        self._seen[key_text] = now
        return True

    def __len__(self) -> int:
        return len(self._seen)


def fallback_decision(
    *,
    records: list[tuple[int, str]],
    host_stt_enabled: bool,
    provider_available: bool,
    plugin_fallback: bool,
    dedup_ok: list[bool],
) -> list[str]:
    """每一段兜底 Record 的**处置结论**（可读原因，适配层照着执行/记录）。

    返回与 ``records`` 等长的字符串列表：空串＝"这一段该兜底转写"；非空＝跳过的
    原因（宿主已开转写却还剩 Record 属异常，交日志，不抢宿主的活；其余各路
    "不该转"的情况给出面板可读的解释）。
    """
    reasons: list[str] = []
    for (_, _), fresh in zip(records, dedup_ok):
        if not fresh:
            reasons.append("同一段语音本轮已兜底转写过，跳过（去重）")
            continue
        if not plugin_fallback:
            reasons.append("插件兜底转写已关闭（voice.listen_fallback=false）")
            continue
        if not provider_available:
            reasons.append("宿主没有已配置的语音识别提供方，无法兜底转写")
            continue
        if host_stt_enabled:
            reasons.append("宿主转写已开启但本段未出文本（转写失败），不再重复转")
            continue
        reasons.append("")  # 该转：宿主没开，插件用宿主的提供方兜底
    return reasons


__all__ = [
    "MAX_KEYS_DEFAULT",
    "MAX_RECORDS_PER_EVENT",
    "TTL_SECONDS_DEFAULT",
    "ListenState",
    "fallback_decision",
    "file_uri_to_path",
    "is_record",
    "pending_records",
    "record_ref",
]
