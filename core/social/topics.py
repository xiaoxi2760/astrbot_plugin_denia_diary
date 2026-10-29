"""群内话题素材：只留**有界的近期**记录，带来源时间。

任务书的边界在这里收口：

- **容量**：每群最多 ``max_items`` 条，单条截到 ``max_chars`` 字（超长群消息
  存个摘要，不存全文）；
- **时效**：读取时按 ``ttl_hours`` 过滤，过期条目在下次写入时顺手清掉——
  长期运行不会攒出一份无限增长的"群史"；
- **去重**：同文本的刷屏只记一条（按指纹）；
- **来源时间**：每条都带 ``ts``，渲染时一并给出（"14:20 谁说了什么"）。

渲染（``trace_line_text`` / ``material_from``）只吃已经裁剪过的条目，
输出长度由调用方的预算再压一道（``compose._fit_lines`` 同款：整行保留）。
隐私边界：本模块**只**吃群消息原文，从不读日记/小本本/私聊记忆——
"私聊恋爱日记里的独特字符串不出现在群素材"由数据来源保证，并有测试钉住。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

def message_hash(speaker_id: str, text: str) -> str:
    """一条群消息的指纹（同消息去重用）：说话人 + 归一化正文。"""
    normalized = " ".join(str(text or "").split())
    return hashlib.sha1(f"{speaker_id}\x00{normalized}".encode("utf-8")).hexdigest()[:16]


def record_message(
    items: Any,
    *,
    ts: str,
    speaker_id: str,
    speaker_name: str,
    text: str,
    max_items: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """把一条群消息并进话题列表 → 新列表（原列表不动）。

    同文本刷屏只记一条；单条截到 ``max_chars``；超过 ``max_items`` 挤掉最旧的。
    """
    body = " ".join(str(text or "").split())
    if not body:
        return list(items) if isinstance(items, list) else []
    digest = message_hash(speaker_id, body)
    existing = [item for item in (items or []) if isinstance(item, dict)]
    if any(str(item.get("hash") or "") == digest for item in existing):
        return existing
    entry = {
        "ts": str(ts),
        "speaker_id": str(speaker_id or ""),
        "speaker_name": str(speaker_name or "")[:60],
        "text": body[: max(1, int(max_chars))],
        "hash": digest,
    }
    keep = list(existing) + [entry]
    return keep[-max(1, int(max_items)):]


def fresh_items(
    items: Any, *, now: datetime, ttl_hours: int
) -> list[dict[str, Any]]:
    """按 TTL 过滤（``ts`` 解析不了的条目一律当过期——宁少勿错）。"""
    out: list[dict[str, Any]] = []
    cutoff = now - timedelta(hours=max(1, int(ttl_hours)))
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        moment = _parse_ts(item.get("ts"))
        if moment is None or moment < cutoff:
            continue
        out.append(item)
    return out


def trace_line_text(items: Any, *, now: datetime, budget: int = 70) -> str:
    """给注入用的短句：``群里最近：14:20 小明说「…」``（整行保留，超预算整行丢弃）。"""
    lines: list[str] = []
    used = 0
    for item in (items or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        moment = _parse_ts(item.get("ts"))
        clock = moment.strftime("%H:%M") if moment else "刚刚"
        who = str(item.get("speaker_name") or "").strip() or "有人"
        line = f"{clock} {who}说「{text}」"
        extra = len(line) + bool(lines)
        if used + extra > budget:
            break
        lines.append(line)
        used += extra
    if not lines:
        return ""
    return "群里最近：" + "；".join(lines)


def material_from(items: Any, *, exclude_speaker: str = "") -> dict[str, Any] | None:
    """巡检候选的素材：最新一条话题（可选排除某说话人）。没有 → None。

    传回原文摘要 + 来源时间；指令在触发器层拼（这里只管"有什么"）。
    """
    for item in reversed(list(items or [])):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if exclude_speaker and str(item.get("speaker_id") or "") == str(exclude_speaker):
            continue
        moment = _parse_ts(item.get("ts"))
        return {
            "text": text,
            "speaker_id": str(item.get("speaker_id") or ""),
            "speaker_name": str(item.get("speaker_name") or ""),
            "ts": item.get("ts") or "",
            "clock": moment.strftime("%H:%M") if moment else "",
        }
    return None


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = ["fresh_items", "material_from", "message_hash", "record_message", "trace_line_text"]
