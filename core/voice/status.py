"""语音能力的**绑定报告**（F4）：面板上每一句"支持 / 不支持"都必须是查证过的事实。

原则（任务验收第 5 条）：**只展示提供方真实支持的能力**。宿主 ``TTSProvider.get_audio``
**只有文本参数**（音色 / 语速 / 情绪全在 provider 配置里写死）——所以"情绪音色控制"
在这个宿主上永远是不支持，面板明说，绝不摆一个假的开关。

事件环（诊断）：最近 20 条语音事件（主动合成 / 被动转写 / 清理）的成败与原因，
进程内、重启即空——它是排障入口，不是账本。

纯函数 + 一个小环形类，零 astrbot 依赖：宿主事实由适配层查好**以字典喂进来**，
这里只负责把事实组织成面板形状，并守住"不知道就写不知道"的诚实底线。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from datetime import datetime
from typing import Any

EVENTS_MAX = 20
"""诊断环容量：面板显示最近 20 条，再多就是日志的事了。"""

DEVICE_VERIFIED = False
"""**未实机验证**：本轮全部离线完成，真机（手机 Ubuntu chroot + QQ 协议端）的
路径映射与文件服务交接需要主人部署后确认——面板固定带这句话。"""

UNTESTED_NOTE = "本面板的可用性判定来自宿主配置与提供方查询，未在手机实机上验证。"

EMOTION_VOICE_REASON = (
    "宿主 TTSProvider.get_audio 只有文本参数（音色/语速/情绪在提供方配置里），"
    "插件无法在调用时切换音色"
)


class VoiceEvents:
    """语音诊断事件环（线程安全的小 deque）：``add`` 记一条，``tail`` 读最近 n 条。"""

    def __init__(self, maxlen: int = EVENTS_MAX) -> None:
        self._lock = threading.Lock()
        self._items: deque[dict[str, Any]] = deque(maxlen=max(1, int(maxlen)))

    def add(
        self,
        kind: str,
        *,
        ok: bool,
        detail: str = "",
        umo: str = "",
        now: datetime | None = None,
        segments: int | None = None,
    ) -> None:
        moment = now or datetime.now()
        entry: dict[str, Any] = {
            "ts": moment.isoformat(timespec="seconds"),
            "kind": str(kind or ""),
            "ok": bool(ok),
            "detail": str(detail or "")[:400],
            "umo": str(umo or ""),
        }
        if segments is not None:
            entry["segments"] = int(segments)
        with self._lock:
            self._items.append(entry)

    def tail(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        if limit is not None:
            items = items[-max(0, int(limit)):]
        return list(reversed(items))  # 面板习惯：最新在前

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _probe_fact(probe: Any, *, what: str) -> dict[str, Any]:
    """规整适配层喂进来的探测结果：``None``＝没查（诚实写"未知"）。"""
    if probe is None:
        return {"available": None, "detail": f"未能探测（{what}）"}
    if isinstance(probe, Mapping):
        available = probe.get("available")
        detail = str(probe.get("detail") or "")
        return {
            "available": None if available is None else bool(available),
            "detail": detail,
        }
    return {"available": bool(probe), "detail": ""}


def report(
    *,
    voice_cfg: Mapping[str, Any] | None,
    tts_probe: Any = None,
    stt_probe: Any = None,
    host_tts_settings: Mapping[str, Any] | None = None,
    host_stt_settings: Mapping[str, Any] | None = None,
    callback_api_base: str = "",
    file_service_registered: bool | None = None,
    events: VoiceEvents | None = None,
) -> dict[str, Any]:
    """组装面板的语音能力报告（不抛异常；事实缺失一律写"未知"，不编）。"""
    cfg = dict(voice_cfg or {})
    tts = _probe_fact(tts_probe, what="宿主没有可查询的 TTS 提供方")
    stt = _probe_fact(stt_probe, what="宿主没有可查询的 STT 提供方")
    host_tts = dict(host_tts_settings or {})
    host_stt = dict(host_stt_settings or {})

    use_file_service = bool(host_tts.get("use_file_service", False))
    base = str(callback_api_base or "").strip()
    file_service: dict[str, Any] = {
        "use_file_service": use_file_service,
        "callback_api_base_set": bool(base),
        "mode": "url" if (use_file_service and base) else "local-path",
        "registered": file_service_registered,
        "detail": (
            "发送时把音频注册成宿主文件服务的限时下载链接（令牌 300 秒、一次性）"
            if use_file_service and base
            else (
                "已要求走文件服务但宿主未配置 callback_api_base，将退回本地路径直发"
                if use_file_service
                else "未启用文件服务：Record 携带宿主本地路径，协议端必须与本进程同文件系统"
            )
        ),
    }

    host_stt_enabled = bool(host_stt.get("enable", False))
    stt_available = stt["available"]
    if stt_available is False:
        listen_effective = "none"
        listen_detail = "宿主没有已配置的语音识别提供方：语音消息她听不懂，会按无文字处理"
    elif host_stt_enabled:
        listen_effective = "host"
        listen_detail = "宿主管线自动转写（Record 就地换成文字），插件不做第二次识别"
    elif stt_available and bool(cfg.get("listen_fallback", True)):
        listen_effective = "plugin-fallback"
        listen_detail = "宿主转写未开：插件在收到语音时用宿主的识别提供方兜底转一次（去重）"
    elif stt_available:
        listen_effective = "none"
        listen_detail = "宿主转写未开且插件兜底已关：语音消息她听不懂"
    else:
        listen_effective = "none"
        listen_detail = str(stt["detail"] or "语音识别可用性未知")

    return {
        "active": {
            "mode": str(cfg.get("mode") or "off"),
            "probability": cfg.get("probability"),
            "tts": {
                "available": tts["available"],
                "stream": None,  # 见下：仅当探测到 provider 实例时才填
                "detail": tts["detail"],
            },
            "emotion_voice": {"supported": False, "reason": EMOTION_VOICE_REASON},
            "max_segments": cfg.get("max_segments"),
            "tts_timeout_seconds": cfg.get("tts_timeout_seconds"),
            "tts_total_seconds": cfg.get("tts_total_seconds"),
        },
        "listen": {
            "effective": listen_effective,
            "detail": listen_detail,
            "host_stt_enabled": host_stt_enabled,
            "provider_available": stt_available,
            "plugin_fallback": bool(cfg.get("listen_fallback", True)),
        },
        "file_service": file_service,
        "device_verified": DEVICE_VERIFIED,
        "untested_note": UNTESTED_NOTE,
        "events": list(events.tail()) if events is not None else [],
    }


def attach_stream_fact(payload: dict[str, Any], provider: Any) -> dict[str, Any]:
    """把 ``provider.support_stream()`` 的**真实返回**补进报告（探测到实例才填）。"""
    active = payload.get("active")
    if not isinstance(active, dict):
        return payload
    tts = active.get("tts")
    if not isinstance(tts, dict):
        return payload
    getter = getattr(provider, "support_stream", None)
    if callable(getter):
        try:
            tts["stream"] = bool(getter())
        except Exception:  # noqa: BLE001 - 能力探测失败就写未知
            tts["stream"] = None
    return payload


__all__ = [
    "DEVICE_VERIFIED",
    "EMOTION_VOICE_REASON",
    "EVENTS_MAX",
    "UNTESTED_NOTE",
    "VoiceEvents",
    "attach_stream_fact",
    "report",
]
