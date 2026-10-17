"""Only prompt injection is sampled; stored notes and tool results stay complete."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable


def _terms(text: str) -> set[str]:
    """Small lexical matcher for Chinese and Latin text, without a tokenizer."""
    terms = set(re.findall(r"[a-z0-9_]{2,}", text.casefold()))
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(word[i : i + 2] for i in range(len(word) - 1))
    return terms


def ranked(
    items: list[dict[str, Any]], *, query: str = "", now: datetime, promises: bool = False
) -> list[dict[str, Any]]:
    """Topic overlap, recent updates and approaching deadlines all contribute."""
    topic = _terms(query)

    def score(item: dict[str, Any]) -> float:
        relevance = min(6, len(topic & _terms(str(item.get("text") or ""))) * 2)
        recent = 0.0
        stamp = item.get("updated_at") or item.get("created_at")
        try:
            age = max(0, (now.date() - datetime.fromisoformat(str(stamp)).date()).days)
            recent = 2 / (1 + age / 7)
        except (TypeError, ValueError):
            pass
        urgency = 0.0
        if promises and item.get("due_at"):
            try:
                days = (datetime.fromisoformat(str(item["due_at"])).date() - now.date()).days
                urgency = 6 if days < 0 else 5 if days == 0 else 3 if days <= 3 else 1 if days <= 7 else 0
            except (TypeError, ValueError):
                pass
        return relevance + recent + urgency

    # Stable ordering retains the caller's urgency order for equal scores.
    return sorted(items, key=score, reverse=True)


def render_entries(
    title: str,
    items: list[dict[str, Any]],
    *,
    format_item: Callable[[dict[str, Any]], str],
    budget: int,
    max_items: int = 3,
) -> str:
    """Keep complete entries; offer a route to the full list when space remains.

    Never slice a note in the middle of a sentence. A long first note must not
    prevent shorter selected notes or the other prompt sections from appearing.
    """
    if not items or budget <= 0 or max_items <= 0:
        return ""
    rows = [f"- {' '.join(format_item(item).split())}" for item in items]
    complete = title + "\n" + "\n".join(rows)
    if len(rows) <= max_items and len(complete) <= budget:
        return complete
    # The note itself has priority over a list-navigation marker. Reserving
    # the marker first can discard a relevant note that fits on its own.
    kept: list[str] = []
    used = len(title)
    for row in rows:
        if len(kept) >= max_items:
            break
        if used + len(row) + 1 <= budget:
            kept.append(row)
            used += len(row) + 1
    omitted = len(rows) - len(kept)
    parts = [title, *kept]
    for marker in (
        f"（另有 {omitted} 条，可用 note_list 翻完整本子。）",
        f"（另有 {omitted} 条未展开。）",
    ):
        if used + len(marker) + 1 <= budget:
            parts.append(marker)
            break
    result = "\n".join(parts)
    return result if len(result) <= budget else ""
