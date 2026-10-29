"""Latest patrol/delivery outcome for the owner panel; never stores message text."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import scope
from .._log import logger
from . import gate


def check_snapshot(proactive: Any, decision: dict | None, *, now: datetime) -> dict:
    """Explain the result of an actual patrol without re-running content finders."""
    result = {"ts": now.isoformat(timespec="seconds"), "status": "idle", "reason": "没有合适素材", "sessions": []}
    if not proactive.settings.subsystem("proactive"):
        result.update(status="disabled", reason="主动消息已关闭")
        return result
    if decision:
        result.update(status="dispatched", reason="已安排发送，等待发送结果", umo=str(decision.get("umo") or ""), slot=str(decision.get("slot") or ""))
        return result
    if proactive.state is not None and proactive.state.is_late_night(now=now):
        result.update(status="quiet", reason="免打扰时段，常规主动消息暂停")
        return result
    doc = proactive.store.read()
    contacts = proactive.store.contacts(doc)
    groups = [str(c.get("umo") or "") for c in contacts.values() if isinstance(c, dict) and c.get("kind") == "group"]
    social = getattr(proactive, "social", None)
    extra = list(social.enabled_group_umos()) if social is not None else []
    targets = scope.proactive_targets(
        str(proactive.settings.scope.get("mode") or ""), contacts=contacts,
        love_peers=proactive.settings.love_peers, sessions=proactive.store.sessions(doc), known_groups=groups,
        extra_groups=extra,
    )
    if not targets:
        result.update(status="no_target", reason="当前启用范围内还没有可联系的会话")
        return result
    allowed = 0
    for umo, kind in targets:
        reason = gate.gate_check(entry=gate.entry_of(doc, umo), kind=kind, cfg=dict(proactive.settings.proactive), late_night=False, now=now)
        allowed += reason is None
        if len(result["sessions"]) < 50:
            result["sessions"].append({"umo": umo, "reason": reason or "没有命中可发送内容，或本轮候选未能提交"})
    if not allowed:
        result.update(status="blocked", reason="所有会话都受到每日上限或最小间隔限制")
    return result


async def record(proactive: Any, snapshot: dict) -> None:
    """One bounded record, shared with the existing atomic proactive store."""
    try:
        def update(doc):
            doc["diagnostic"] = dict(snapshot)
            return doc
        await proactive.store.update(update)
    except Exception as error:
        logger.warning("[proactive] 巡检结果记录失败：%s", error)


async def record_check(proactive: Any, decision: dict | None, *, now: datetime) -> None:
    """The optional explanation must never prevent dispatching a committed decision."""
    try:
        snapshot = check_snapshot(proactive, decision, now=now)
    except Exception as error:
        logger.warning("[proactive] 巡检结果计算失败：%s", error)
        snapshot = {"ts": now.isoformat(timespec="seconds"), "status": "unavailable", "reason": "巡检说明暂不可用，请查看日志", "sessions": []}
    await record(proactive, snapshot)


async def delivery(proactive: Any, umo: str, *, success: bool, now: datetime, reason: str = "") -> None:
    await record(proactive, {
        "ts": now.isoformat(timespec="seconds"), "status": "sent" if success else "failed",
        "reason": ("平台已确认发送成功" if success else "发送失败，未确认送达") + ("：" + reason[:160] if reason else ""),
        "umo": umo, "sessions": [],
    })
