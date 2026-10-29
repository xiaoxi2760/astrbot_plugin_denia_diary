"""群与参与者的登记（身份按**真实 id**，不按昵称猜）。

三条硬规矩（任务书 + 用户拍板）：

1. **身份 = 平台 + 真实 id**。群的键是 umo（``平台:GroupMessage:群号``），参与者
   的键是平台用户 id。显示名只是展示字段——改名、重名、撞昵称都不影响身份判定，
   自动观察也**只**落真实 id，昵称仅作为 display 记录；
2. **默认不开口**。新登记的群 ``enabled=False``，新登记的参与者
   ``allow_interaction=False``——"主人指定的群"才可能被主动发言，且每个参与者
   还要主人逐个允许；
3. **是人是 bot 由主人标注**。自动观察**绝不**从昵称/行为猜 ``is_bot``；
   自动补录的参与者一律先按"人"记（``is_bot=False``），主人在面板上改。

全部纯函数 + 数据规范化，零平台依赖；超限（群数/名单长度）在这里拒绝并给原因。
"""

from __future__ import annotations

import re
from typing import Any

GROUP_MESSAGE_TYPE = "GroupMessage"
"""AstrBot 统一消息源的群聊 message_type 段（``Session.from_umo`` 的三段式）。"""

MAX_NAME = 60
"""显示名长度上限：它是给人看的，超长就地截断。"""

_MAX_ID = 120
"""真实 id 上限：正常平台 id 远短于这个；超长按"不像 id"拒绝，不截断——
身份字段宁可拒绝也不能存半个。"""

_ID_RE = re.compile(r"^[^\s:]+$")
"""id / 平台名的形状约束：非空、无空白、无冒号（umo 三段式用它分段）。"""

_UMO_RE = re.compile(r"^\S+$")
"""群 umo：非空、无空白（它本身就含冒号，交给 ``parse_umo`` 拆）。"""


def _clean_umo(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 200 or not _UMO_RE.match(text):
        return ""
    return text


def group_umo(platform: str, group_id: str) -> str:
    """``平台 + 群号`` → 群 umo（与宿主 ``unified_msg_origin`` 的三段式一致）。"""
    return f"{platform}:{GROUP_MESSAGE_TYPE}:{group_id}"


def parse_umo(umo: str) -> tuple[str, str, str]:
    """群 umo → ``(platform, message_type, session_id)``；解析不出给空串。"""
    parts = str(umo or "").split(":")
    if len(parts) != 3 or not all(parts):
        return "", "", ""
    return parts[0], parts[1], parts[2]


def _clean_text(value: Any, limit: int = MAX_NAME) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text[:limit]


def _clean_id(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())
    if not text or len(text) > _MAX_ID or not _ID_RE.match(text):
        return ""
    return text


def normalize_group(
    raw: Any, *, now: str, existing: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """登记/更新一个群 → ``(记录, 原因)``。``记录 is None`` 即拒绝（原因给人看）。

    接受两种写法：直接给 ``umo``（面板从"待登记"一键带过来的），或给
    ``platform`` + ``group_id``（主人手填）。``enabled`` 缺省 **False**；
    更新时未提供的键沿用旧值（改名不改开关，改开关不改名）。
    """
    body = raw if isinstance(raw, dict) else {}
    umo = _clean_umo(body.get("umo") or "")
    platform = _clean_id(body.get("platform") or "")
    group_id = _clean_id(body.get("group_id") or "")
    if not umo:
        if not platform or not group_id:
            return None, "需要 platform + group_id（或直接给群 umo）"
        umo = group_umo(platform, group_id)
    else:
        platform, message_type, group_id = parse_umo(umo)
        if not platform or message_type != GROUP_MESSAGE_TYPE or not group_id:
            return None, f"群 umo 形状不对（要 平台:GroupMessage:群号）：{umo!r}"
    base = dict(existing) if isinstance(existing, dict) else {}
    record: dict[str, Any] = {
        "umo": umo,
        "platform": platform,
        "group_id": group_id,
        "name": _clean_text(body.get("name")) or str(base.get("name") or ""),
        "enabled": bool(base.get("enabled", False)) if "enabled" not in body else bool(body.get("enabled")),
        "registered_at": str(base.get("registered_at") or now),
        "updated_at": now,
    }
    return record, ""


def normalize_participant(
    raw: Any, *, now: str, existing: dict[str, Any] | None = None
) -> tuple[dict[str, Any] | None, str]:
    """登记/更新一个参与者 → ``(记录, 原因)``。身份 = 真实 id；人/bot 由主人标注。"""
    body = raw if isinstance(raw, dict) else {}
    pid = _clean_id(body.get("id") or body.get("person_id") or "")
    if not pid:
        return None, "需要参与者的真实 id（不按昵称猜）"
    base = dict(existing) if isinstance(existing, dict) else {}
    if "is_bot" in body:
        is_bot = bool(body.get("is_bot"))
    elif "is_bot" in base:
        is_bot = bool(base.get("is_bot"))
    else:
        is_bot = False  # 自动补录/漏标一律先按"人"记，主人确认是 bot 再改
    record: dict[str, Any] = {
        "id": pid,
        "name": _clean_text(body.get("name")) or str(base.get("name") or ""),
        "is_bot": is_bot,
        "allow_interaction": bool(base.get("allow_interaction", False))
        if "allow_interaction" not in body
        else bool(body.get("allow_interaction")),
        "registered_at": str(base.get("registered_at") or now),
        "updated_at": now,
    }
    return record, ""


def group_enabled(doc: dict[str, Any], umo: str) -> bool:
    """这个群是否已登记**且**启用（主动发言 / 话题观察的总闸）。"""
    group = (doc.get("groups") or {}).get(str(umo or ""))
    return bool(isinstance(group, dict) and group.get("enabled"))


def enabled_group_umos(doc: dict[str, Any]) -> list[str]:
    """已登记且启用的群 umo（插入序稳定）。"""
    groups = doc.get("groups")
    if not isinstance(groups, dict):
        return []
    return [str(umo) for umo, g in groups.items() if isinstance(g, dict) and g.get("enabled")]


def participant_of(doc: dict[str, Any], umo: str, pid: str) -> dict[str, Any] | None:
    """某个群里某个参与者的登记（没登记 → None）。"""
    participants = (doc.get("participants") or {}).get(str(umo or ""))
    if not isinstance(participants, dict):
        return None
    entry = participants.get(str(pid or ""))
    return entry if isinstance(entry, dict) else None


__all__ = [
    "GROUP_MESSAGE_TYPE",
    "MAX_NAME",
    "enabled_group_umos",
    "group_enabled",
    "group_umo",
    "normalize_group",
    "normalize_participant",
    "parse_umo",
    "participant_of",
]
