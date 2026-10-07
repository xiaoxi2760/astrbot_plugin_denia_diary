"""配置：默认值、取值范围、校验与降级（第 0 步产物）。

约定：

- 面板传入的原始配置（dict）**只读一次**，转成不可变的 ``Settings``；
- 越界即夹紧、非法即回落默认并记 warning，**不抛异常**（缺省即降级）；
- 内核不缓存派生值——要用什么就从 ``Settings`` 现取（面板保存会热重载插件）；
- 只依赖标准库，可离线测试；取值范围是"用户能调多少"，格式契约里的常量
  （如日记头行长度上限）属于 ``diary/format.py``，不在这里。

零第三方依赖；不 import astrbot。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._log import logger
from .outbound import DEFAULT_MARKER_PATTERN
from .scope import DEFAULT_MODE, normalize_mode

DEFAULT_TIMEZONE = "Asia/Shanghai"
"""默认时区。禁止裸 ``datetime.now()``：云端多为 UTC，会整体错 8 小时。"""

SUBSYSTEMS: tuple[str, ...] = ("diary", "notebook", "state", "proactive")
"""子系统开关名（调试期可只关一块）。"""

INT_LIMITS: dict[str, tuple[int, int]] = {
    "diary.edit_within_days": (1, 60),
    "diary.max_chars": (200, 20000),
    "diary.nudge_after_hour": (0, 23),
    "diary.soft_nudge_after_hour": (0, 23),
    "diary.event_hint_cooldown_min": (5, 24 * 60),
    "diary.fallback_hint_cooldown_min": (15, 24 * 60),
    "diary.exchange_window_min": (5, 24 * 60),
    "notebook.promise_limit": (1, 200),
    "notebook.fact_limit": (1, 200),
    "proactive.patrol_minutes": (1, 59),
    "proactive.daily_limit_private": (1, 10),
    "proactive.daily_limit_group": (0, 10),
    "proactive.min_interval_minutes": (0, 1440),
    "proactive.signal_cooldown_days": (1, 30),
    "proactive.pending_window_hours": (1, 48),
    "proactive.quiet_days": (1, 30),
}

_DIARY_DEFAULTS: dict[str, int] = {
    "edit_within_days": 7,
    "max_chars": 1200,
    "nudge_after_hour": 22,
    "soft_nudge_after_hour": 19,
    "event_hint_cooldown_min": 30,
    "fallback_hint_cooldown_min": 90,
    "exchange_window_min": 30,
}

# /看日记 图片卡（第 14 步）的两个字符串档位。语义的唯一解释住在 main.py 的
# 渲染降级链——这里只管"取值合不合法"，和 scope.mode 一样的分权方式。
CARD_RENDER_CHOICES: tuple[str, ...] = ("pretty", "plain", "off")
"""pretty=HTML 模板（要联网走文转图端点）；plain=Markdown 渲染（可离线）；off=只发文本。"""
CARD_RENDER_DEFAULT = "pretty"
CARD_STYLE_CHOICES: tuple[str, ...] = ("paper", "ink", "postcard")
"""pretty 档的模板款式：paper=纸感（面板同款，默认）；ink=墨信；postcard=明信片。"""
CARD_STYLE_DEFAULT = "paper"

_NOTEBOOK_DEFAULTS: dict[str, int] = {
    "promise_limit": 20,  # 约定上限（每人，按未完成计）
    "fact_limit": 15,  # 事实上限（每人）
}

_OUTBOUND_DEFAULTS: dict[str, Any] = {
    "strip_meme_marks": True,          # 开关：默认剥
    "marker_pattern": DEFAULT_MARKER_PATTERN,  # 只剥 &&名字&& 这一种无歧义形态
}
"""出站清洗组（第 5.1 步）。默认值的**唯一来源**在 ``core.outbound``——
这里只声明"有哪几个键"，不重新发明正则。"""

OUTBOUND_DEFAULTS = _OUTBOUND_DEFAULTS
"""只读别名（给测试与外部引用用；改它就是改上面的默认表）。"""

_DEFAULT_RHYTHM = (
    "06:00|刚醒\n"
    "09:00|精神不错\n"
    "13:00|有点犯困\n"
    "18:00|晚饭后放松\n"
    "23:00|该睡了"
)
"""作息表默认值：每行「HH:MM|状态词」，到点的最近一段生效，凌晨归入最后一段。"""

_STATE_DEFAULTS: dict[str, str] = {
    "rhythm": _DEFAULT_RHYTHM,
    "rhythm_weekend": "",  # 周末表：留空＝沿用工作日那张（默认单表）
    "late_night": "23:30-06:30",  # 深夜时段（可跨午夜）；留空＝永不深夜
}

_SCOPE_DEFAULTS: dict[str, str] = {
    "mode": DEFAULT_MODE,  # 默认「只私聊」：群聊的记录/注入从这一步起默认关闭（用户拍板）
}
"""启用范围组（第 7 步）：判定与语义的唯一来源在 ``core.scope``——这里只声明键与默认值。"""

_PANEL_DEFAULTS: dict[str, str] = {
    "brand": "情绪日记",  # 面板左上角标题（可改成任意称呼）
    "brand_sub": "观察面板",  # 标题下面那行小字
}
"""面板外观组（第 10 步）：只驱动 WebUI 显示，**不进任何提示词、不参与判定**。
留空串是有意义的值（＝那行不显示），所以缺键才回落默认——与 ``state``/``proactive`` 同规矩。"""

MAX_PANEL_TEXT = 60
"""面板文案长度上限：它是显示层的东西，超长的值会把侧栏撑坏，就地截断并记 warning。"""

# 频率与冷却默认值 = 第 4 步任务书 §2 的用户定稿值（§5 工期纪律：全部待调）。
# 暗号触发阈值（当下 valence ≤ -0.7，判衰减后坐标）与各触发器的时段窗口是代码
# 常量，不进配置——前者要浮点（面板 int 项装不下），后者属于"判定阈值"不是"频率"。
_PROACTIVE_DEFAULTS: dict[str, Any] = {
    "patrol_minutes": 15,  # 巡检间隔（cron 表达式分钟步长，1-59）
    "daily_limit_private": 2,  # 私聊每日上限
    "daily_limit_group": 1,  # 群聊每日上限
    "min_interval_minutes": 120,  # 同会话最小间隔
    "signal_cooldown_days": 3,  # 暗号独立冷却
    "pending_window_hours": 6,  # 待回窗口（喂 trace_line）
    "quiet_days": 3,  # 久未联系阈值（天，待调）
    "signal_char": "。",  # 暗号字符（1~4 个字符）
}


def default_config() -> dict[str, Any]:
    """一份全新的默认配置（每次调用返回新对象，避免共享可变默认值）。"""
    return {
        "enabled": True,
        "timezone": DEFAULT_TIMEZONE,
        "data_dir": "",
        "subsystems": dict.fromkeys(SUBSYSTEMS, True),
        "scope": dict(_SCOPE_DEFAULTS),
        "panel": dict(_PANEL_DEFAULTS),
        "diary": {**dict(_DIARY_DEFAULTS), "card_render": CARD_RENDER_DEFAULT, "card_style": CARD_STYLE_DEFAULT},
        "notebook": dict(_NOTEBOOK_DEFAULTS),
        "state": dict(_STATE_DEFAULTS),
        "proactive": dict(_PROACTIVE_DEFAULTS),
        "outbound": dict(_OUTBOUND_DEFAULTS),
        "love_peers": [],
        "name_preference": {},
    }


@dataclass(frozen=True)
class Settings:
    """校验后的配置快照。字段全部只读；``warnings`` 记录降级原因，供自检/日志展示。"""

    enabled: bool = True
    timezone: str = DEFAULT_TIMEZONE
    data_dir: str = ""
    subsystems: Mapping[str, bool] = field(default_factory=lambda: dict.fromkeys(SUBSYSTEMS, True))
    scope: Mapping[str, str] = field(default_factory=lambda: dict(_SCOPE_DEFAULTS))
    panel: Mapping[str, str] = field(default_factory=lambda: dict(_PANEL_DEFAULTS))
    diary: Mapping[str, Any] = field(default_factory=lambda: {
        **dict(_DIARY_DEFAULTS), "card_render": CARD_RENDER_DEFAULT, "card_style": CARD_STYLE_DEFAULT,
    })
    notebook: Mapping[str, int] = field(default_factory=lambda: dict(_NOTEBOOK_DEFAULTS))
    state: Mapping[str, str] = field(default_factory=lambda: dict(_STATE_DEFAULTS))
    proactive: Mapping[str, Any] = field(default_factory=lambda: dict(_PROACTIVE_DEFAULTS))
    outbound: Mapping[str, Any] = field(default_factory=lambda: dict(_OUTBOUND_DEFAULTS))
    love_peers: tuple[str, ...] = ()
    name_preference: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def subsystem(self, name: str) -> bool:
        """某子系统是否可用（总开关关闭时一律 False）。"""
        return self.enabled and bool(self.subsystems.get(name, False))

    def zone(self) -> ZoneInfo:
        """当前时区对象。"""
        return ZoneInfo(self.timezone)


def load_settings(raw: Mapping[str, Any] | None) -> Settings:
    """把原始配置转成 ``Settings``：未知项忽略、越界夹紧、非法回落，全部记进 ``warnings``。"""
    warnings: list[str] = []
    if raw is None:
        src: Mapping[str, Any] = {}
    elif isinstance(raw, Mapping):
        src = raw
    else:
        warnings.append(f"配置不是对象（{type(raw).__name__}），全部使用默认值")
        src = {}

    unknown = sorted(set(src) - set(default_config()))
    if unknown:
        warnings.append("忽略未知配置项：" + "、".join(unknown))

    enabled = _as_bool(src.get("enabled"), True, "enabled", warnings)
    timezone = _as_timezone(src.get("timezone"), warnings)
    data_dir = _as_text(src.get("data_dir"))

    raw_subsystems = src.get("subsystems")
    if raw_subsystems is not None and not isinstance(raw_subsystems, Mapping):
        warnings.append("subsystems 不是对象，全部使用默认值（开）")
    subsystems = {
        name: _as_bool(
            raw_subsystems.get(name) if isinstance(raw_subsystems, Mapping) else None,
            True,
            f"subsystems.{name}",
            warnings,
        )
        for name in SUBSYSTEMS
    }

    raw_diary = src.get("diary")
    if raw_diary is not None and not isinstance(raw_diary, Mapping):
        warnings.append("diary 不是对象，全部使用默认值")
    diary = {
        key: _as_int(
            raw_diary.get(key) if isinstance(raw_diary, Mapping) else None,
            default,
            f"diary.{key}",
            warnings,
        )
        for key, default in _DIARY_DEFAULTS.items()
    }
    # /看日记 图片卡的两个档位（字符串）：缺键静默用默认，写了不合法的值才 warning
    diary["card_render"] = _as_choice(
        raw_diary.get("card_render") if isinstance(raw_diary, Mapping) else None,
        CARD_RENDER_CHOICES, CARD_RENDER_DEFAULT, "diary.card_render", warnings,
    )
    diary["card_style"] = _as_choice(
        raw_diary.get("card_style") if isinstance(raw_diary, Mapping) else None,
        CARD_STYLE_CHOICES, CARD_STYLE_DEFAULT, "diary.card_style", warnings,
    )

    raw_notebook = src.get("notebook")
    if raw_notebook is not None and not isinstance(raw_notebook, Mapping):
        warnings.append("notebook 不是对象，全部使用默认值")
    notebook = {
        key: _as_int(
            raw_notebook.get(key) if isinstance(raw_notebook, Mapping) else None,
            default,
            f"notebook.{key}",
            warnings,
        )
        for key, default in _NOTEBOOK_DEFAULTS.items()
    }

    state = _as_state(src.get("state"), warnings)

    scope = _as_scope(src.get("scope"), warnings)

    panel = _as_panel(src.get("panel"), warnings)

    proactive = _as_proactive(src.get("proactive"), warnings)

    outbound = _as_outbound(src.get("outbound"), warnings)

    love_peers = _as_ids(src.get("love_peers"), warnings)
    name_preference = _as_preference(src.get("name_preference"), warnings)

    for item in warnings:
        logger.warning("[settings] %s", item)

    return Settings(
        enabled=enabled,
        timezone=timezone,
        data_dir=data_dir,
        subsystems=subsystems,
        scope=dict(scope),
        panel=dict(panel),
        diary=dict(diary),
        notebook=dict(notebook),
        state=dict(state),
        proactive=dict(proactive),
        outbound=dict(outbound),
        love_peers=love_peers,
        name_preference=dict(name_preference),
        warnings=tuple(warnings),
    )


# ---- 单项处理 ----------------------------------------------------------------


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_bool(value: Any, default: bool, field_name: str, warnings: list[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    warnings.append(f"{field_name} 不是布尔值（{value!r}），使用默认 {default}")
    return default


def _as_int(value: Any, default: int, field_name: str, warnings: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        warnings.append(f"{field_name} 不是整数（{value!r}），使用默认 {default}")
        return default
    low, high = INT_LIMITS[field_name]
    if value < low or value > high:
        warnings.append(f"{field_name}={value} 超出 [{low}, {high}]，已夹紧")
        return min(max(value, low), high)
    return value


def _as_timezone(value: Any, warnings: list[str]) -> str:
    name = _as_text(value) or DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        warnings.append(f"时区 {name!r} 无效，回落到 {DEFAULT_TIMEZONE}")
        return DEFAULT_TIMEZONE
    return name


def _as_state(value: Any, warnings: list[str]) -> dict[str, str]:
    """状态组的三个字符串配置（作息表 / 周末表 / 深夜窗）：非字符串回落默认。"""
    if value is not None and not isinstance(value, Mapping):
        warnings.append("state 不是对象，全部使用默认值")
    out: dict[str, str] = {}
    for key, default in _STATE_DEFAULTS.items():
        raw = value.get(key) if isinstance(value, Mapping) else None
        if raw is None:
            out[key] = default
        elif isinstance(raw, str):
            out[key] = raw.strip()
        else:
            warnings.append(f"state.{key} 不是字符串（{raw!r}），使用默认")
            out[key] = default
    return out


def _as_scope(value: Any, warnings: list[str]) -> dict[str, str]:
    """启用范围组（第 7 步）：一个 ``mode`` 字段。取值判定住在 ``core.scope.normalize_mode``
    （未知回落默认 + warning），这里只接线——判定逻辑写两份必然漂移。"""
    if value is not None and not isinstance(value, Mapping):
        warnings.append("scope 不是对象，全部使用默认值")
    raw = value.get("mode") if isinstance(value, Mapping) else None
    mode, warning = normalize_mode(raw)
    if warning:
        warnings.append(warning)
    return {"mode": mode}


def _as_choice(
    value: Any,
    allowed: tuple[str, ...],
    default: str,
    name: str,
    warnings: list[str],
) -> str:
    """枚举字符串的通用校验：命中返回原值；空/缺静默回落默认（缺键不是错误）；
    写了不合法的值回落默认并记 warning。与 ``normalize_mode`` 的口径一致。"""
    text = str(value).strip() if isinstance(value, str) else ""
    if text in allowed:
        return text
    if text:
        warnings.append(f"{name} 非法（{text}），回落 {default}")
    return default


def _as_panel(value: Any, warnings: list[str]) -> dict[str, str]:
    """面板外观组（第 10 步）：两个纯展示字符串。

    空串是**合法值**（＝那行不显示），所以只有"缺键"才回落默认；非字符串回落默认并记
    warning，超长就地截断——这一段坏了顶多让侧栏难看，绝不该连带把面板弄崩。
    """
    if value is not None and not isinstance(value, Mapping):
        warnings.append("panel 不是对象，全部使用默认值")
    out: dict[str, str] = {}
    for key, default in _PANEL_DEFAULTS.items():
        raw = value.get(key) if isinstance(value, Mapping) else None
        if raw is None:
            out[key] = default
        elif not isinstance(raw, str):
            warnings.append(f"panel.{key} 不是字符串（{raw!r}），使用默认")
            out[key] = default
        else:
            text = raw.strip()
            if len(text) > MAX_PANEL_TEXT:
                warnings.append(f"panel.{key} 超过 {MAX_PANEL_TEXT} 字，已截断")
                text = text[:MAX_PANEL_TEXT]
            out[key] = text
    return out


def _as_proactive(value: Any, warnings: list[str]) -> dict[str, Any]:
    """主动消息组：整数项按 ``INT_LIMITS`` 夹紧，``signal_char`` 是 1~4 个字符的短串。"""
    if value is not None and not isinstance(value, Mapping):
        warnings.append("proactive 不是对象，全部使用默认值")
    out: dict[str, Any] = {}
    for key, default in _PROACTIVE_DEFAULTS.items():
        raw = value.get(key) if isinstance(value, Mapping) else None
        if raw is None:
            out[key] = default
        elif isinstance(default, str):
            if not isinstance(raw, str):
                warnings.append(f"proactive.{key} 不是字符串（{raw!r}），使用默认")
                out[key] = default
            else:
                text = raw.strip()
                if not text or len(text) > 4:
                    warnings.append(f"proactive.{key} 应是 1~4 个字符（{raw!r}），使用默认")
                    out[key] = default
                else:
                    out[key] = text
        else:
            out[key] = _as_int(raw, default, f"proactive.{key}", warnings)
    return out


def _as_outbound(value: Any, warnings: list[str]) -> dict[str, Any]:
    """出站清洗组（第 5.1 步）：开关 + 标记正则。

    非法**一律回落默认并记 warning**，绝不抛异常——配置坏了也不能让她发不出消息。
    正则额外做一次"能匹配空串"的检查：零宽模式会把整条消息清空，属于危险配置。
    """
    if value is not None and not isinstance(value, Mapping):
        warnings.append("outbound 不是对象，全部使用默认值")
    out: dict[str, Any] = {}
    out["strip_meme_marks"] = _as_bool(
        value.get("strip_meme_marks") if isinstance(value, Mapping) else None,
        True,
        "outbound.strip_meme_marks",
        warnings,
    )
    raw = value.get("marker_pattern") if isinstance(value, Mapping) else None
    out["marker_pattern"] = _as_pattern(raw, DEFAULT_MARKER_PATTERN, "outbound.marker_pattern", warnings)
    return out


def _as_pattern(value: Any, default: str, field_name: str, warnings: list[str]) -> str:
    """正则字符串：空/非字符串/编译失败/零宽，全部回落默认。"""
    if value is None:
        return default
    if not isinstance(value, str):
        warnings.append(f"{field_name} 不是字符串（{value!r}），使用默认")
        return default
    text = value.strip()
    if not text:
        warnings.append(f"{field_name} 是空字符串，使用默认")
        return default
    try:
        compiled = re.compile(text)
    except re.error as exc:
        warnings.append(f"{field_name} 不是合法正则（{exc}），使用默认")
        return default
    if compiled.search("") is not None:
        warnings.append(f"{field_name} 能匹配空串（会把消息清空），使用默认")
        return default
    return text


def _as_ids(value: Any, warnings: list[str]) -> tuple[str, ...]:
    """id 名单（如 love_peers）：去空、去重、保序。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        warnings.append(f"id 名单不是列表（{type(value).__name__}），已忽略")
        return ()
    out: list[str] = []
    for item in value:
        text = _as_text(item) if isinstance(item, str) else (str(item).strip() if item is not None else "")
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _as_preference(value: Any, warnings: list[str]) -> Mapping[str, str]:
    """``{id: 称呼}``：非字符串值转字符串，空称呼丢弃。"""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        warnings.append(f"name_preference 不是对象（{type(value).__name__}），已忽略")
        return {}
    out: dict[str, str] = {}
    for key, val in value.items():
        name = str(key).strip()
        text = str(val).strip() if val is not None else ""
        if name and text:
            out[name] = text
    return out


__all__ = [
    "DEFAULT_TIMEZONE",
    "INT_LIMITS",
    "MAX_PANEL_TEXT",
    "OUTBOUND_DEFAULTS",
    "SUBSYSTEMS",
    "Settings",
    "default_config",
    "load_settings",
]
