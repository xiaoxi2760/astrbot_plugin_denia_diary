"""小本本语义入口（第 2 步）：``note_add`` / ``note_complete`` / ``note_forget`` /
``note_list`` / ``facts_for`` / ``promises_for`` / ``promises_due`` / 注入文案。

**判定全部收在入口内部**（方案 §十 规矩 1，原包 P1-a 的教训）：

- ``enabled`` 开关：关了**读也读不到**（不只是挡写）；
- 上限：满了返回「本子满了，先删掉或合并几条再来」——不报错，把精简动作交给她自己；
  "合并"没有专门工具，就是删旧（note_forget）+ 写新（note_add）；
- 可见性（§4.2，v0.2 收紧）：
  - **事实**只在与该人的**私聊**注入——同一个群 ≠ 该人在场，群里一律不出内容；
  - **约定**在私聊出"关于眼前这个人"的未完成全文（过期排前）；群里只出一条
    脱敏计数行（"你还有 N 件答应过的事没办完"），不带到内容；
  - **改动**（完成 / 删）按条目归属判定：``about == session.person_id()`` 才允许动，
    私聊群聊同一套——判据收口在 ``session.py`` 的 ``person_id()``；
- **记录不限会话**：群聊也能记（§4.2 只限制注入，不限制记录），``about`` 缺省即眼前人。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import storage
from ..session import Session
from .store import NotebookStore

DISABLED = "小本本功能已关闭"
FULL_MESSAGE = "本子满了，先删掉或合并几条再来"
NOT_FOUND = "小本本里没找到这一条（它可能不在眼前这个人名下，或已经删了）"

KIND_FACT = "fact"
KIND_PROMISE = "promise"
_KINDS: dict[str, str] = {
    "fact": KIND_FACT,
    "事实": KIND_FACT,
    "promise": KIND_PROMISE,
    "约定": KIND_PROMISE,
}


@dataclass
class Notebook:
    """小本本门面。配置、布局、文件层都由调用方注入（内核不认平台，也不认全局单例）。"""

    settings: object
    layout: storage.Layout
    store: NotebookStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)

    # ---- 内部 ---------------------------------------------------------------

    def _enabled(self) -> bool:
        return bool(self.settings.subsystem("notebook"))

    def _tz(self):
        return self.settings.zone()

    def _limits(self) -> dict[str, int]:
        return dict(self.settings.notebook)

    def _stamp(self, moment: datetime) -> str:
        return moment.isoformat(timespec="seconds")

    def _name(self, peer_id: str) -> str:
        pref = dict(self.settings.name_preference or {})
        return pref.get(peer_id) or peer_id

    def _person(self, session: Session) -> str:
        return session.person_id()

    # ---- 读（可见性判定写在这里） ---------------------------------------------

    def facts_for(self, session: Session) -> list[dict[str, Any]]:
        """某人的事实清单。**只在私聊可见**：群聊一律空（同一个群 ≠ 该人在场）。"""
        if not self._enabled():
            return []
        if not session.is_private:
            return []
        who = self._person(session)
        if not who:
            return []
        doc = self.store.read()
        return [dict(item) for item in (doc.get("facts") or {}).get(who) or []]

    def promises_for(self, session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """关于眼前这个人的**未完成**约定（过期排前）。群聊一律空（脱敏见 ``promise_line``）。"""
        if not self._enabled():
            return []
        if not session.is_private:
            return []
        who = self._person(session)
        if not who:
            return []
        doc = self.store.read()
        mine = [
            dict(item)
            for item in doc.get("promises") or []
            if str(item.get("about") or "") == who and not item.get("done_at")
        ]
        return self._by_urgency(mine, now=now)

    def promises_due(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """到期 / 已过期的未完成约定（全局，给第 4 步主动消息取料用）。"""
        if not self._enabled():
            return []
        moment = now or datetime.now(self._tz())
        today = moment.date().isoformat()
        doc = self.store.read()
        due = [
            dict(item)
            for item in doc.get("promises") or []
            if not item.get("done_at") and item.get("due_at") and str(item["due_at"]) <= today
        ]
        return self._by_urgency(due, now=now)

    def _open_count(self) -> int:
        """全局未完成约定数（群聊脱敏行只用数，不用内容）。"""
        doc = self.store.read()
        return sum(1 for item in doc.get("promises") or [] if not item.get("done_at"))

    @staticmethod
    def _by_urgency(items: list[dict[str, Any]], *, now: datetime | None = None) -> list[dict[str, Any]]:
        """排序：已过期 → 有期限（近的先） → 无期限；同批按各自日期（无期限按创建时间）。"""
        today = now.date().isoformat() if isinstance(now, datetime) else ""

        def key(item: dict[str, Any]) -> tuple[int, str]:
            due = str(item.get("due_at") or "")
            if due:
                if today and due <= today:
                    return (0, due)  # 已过期排最前
                return (1, due)
            return (2, str(item.get("created_at") or ""))

        return sorted(items, key=key)

    # ---- 写 ------------------------------------------------------------------

    async def note_add(
        self,
        session: Session,
        *,
        kind: object,
        text: object,
        about: object = "",
        due_at: object = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """记一条。群聊私聊都行，``about`` 缺省即眼前人（``Session.person_id()``）。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        kind_name = _KINDS.get(str(kind or "").strip().lower()) or _KINDS.get(str(kind or "").strip())
        if kind_name is None:
            return {
                "ok": False,
                "error": "只记两类：fact ＝ 关于对方的重要事实；promise ＝ 约定（准备做 / 约好了做什么）。",
            }
        body = str(text or "").strip()
        if not body:
            return {"ok": False, "error": "要记的事是空的——把内容写进 text。"}
        who = str(about or "").strip() or self._person(session)
        if not who:
            return {"ok": False, "error": "不知道这条记给谁：把对方的 id 放进 about。"}
        stamp = self._stamp(moment)
        limits = self._limits()

        if kind_name == KIND_PROMISE:
            due = str(due_at or "").strip()
            if due and not _is_day(due):
                return {"ok": False, "error": "期限要写成 YYYY-MM-DD（如 2026-10-12），或者空着不填。"}
            entry = {
                "id": _new_id(),
                "text": body,
                "about": who,
                "created_at": stamp,
                "due_at": due or None,
                "done_at": None,
            }
            result = await self.store.add_promise(entry, limit=limits["promise_limit"])
            if not result.get("ok"):
                return {"ok": False, "error": FULL_MESSAGE, "reason": result.get("reason")}
            return {
                "ok": True,
                "kind": KIND_PROMISE,
                "id": entry["id"],
                "about": who,
                "about_name": self._name(who),
                "due_at": entry["due_at"],
                "count": result["count"],
                "limit": limits["promise_limit"],
                "text": body,
            }

        entry = {"id": _new_id(), "text": body, "created_at": stamp, "updated_at": stamp}
        result = await self.store.add_fact(who, entry, limit=limits["fact_limit"])
        if not result.get("ok"):
            return {"ok": False, "error": FULL_MESSAGE, "reason": result.get("reason")}
        return {
            "ok": True,
            "kind": KIND_FACT,
            "id": entry["id"],
            "about": who,
            "about_name": self._name(who),
            "count": result["count"],
            "limit": limits["fact_limit"],
            "text": body,
        }

    # ---- 改 / 删（按条目归属判定，私聊群聊同一套） ------------------------------

    async def note_complete(
        self, session: Session, note_id: object, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """把约定标记为完成。事实没有完成语义；别人的条目动不了。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        if self._owned_fact(session, note_id) is not None:
            return {"ok": False, "error": "事实没有「完成」一说——要删就用 note_forget。"}
        target = self._owned_promise(session, note_id)
        if target is None:
            return {"ok": False, "error": NOT_FOUND}
        if target.get("done_at"):
            return {"ok": False, "error": "这条约定早就完成了。"}
        result = await self.store.mark_promise_done(str(note_id or ""), self._stamp(moment))
        if not result.get("ok"):
            return {"ok": False, "error": NOT_FOUND}  # 预检和落盘之间被删（WebUI / 并发）
        return {"ok": True, "kind": KIND_PROMISE, "id": target["id"], "text": target["text"]}

    async def note_forget(
        self, session: Session, note_id: object, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """删一条（事实或约定）：先过归属判定，再移进回收站。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        moment = now or datetime.now(self._tz())
        target = self._owned_entry(session, note_id)
        if target is None:
            return {"ok": False, "error": NOT_FOUND}
        result = await self.store.forget(str(note_id or ""), deleted_at=self._stamp(moment))
        if not result.get("ok"):
            return {"ok": False, "error": NOT_FOUND}
        return {
            "ok": True,
            "kind": result["kind"],
            "id": str(note_id or ""),
            "text": result["entry"].get("text") or "",
        }

    def _owned_promise(self, session: Session, note_id: object) -> dict[str, Any] | None:
        """归属判定：``about == 眼前人`` 的约定才算"你的本子上的"。"""
        who = self._person(session)
        if not who:
            return None
        doc = self.store.read()
        for item in doc.get("promises") or []:
            if str(item.get("id") or "") == str(note_id or ""):
                return dict(item) if str(item.get("about") or "") == who else None
        return None

    def _owned_fact(self, session: Session, note_id: object) -> dict[str, Any] | None:
        who = self._person(session)
        if not who:
            return None
        doc = self.store.read()
        for item in (doc.get("facts") or {}).get(who) or []:
            if str(item.get("id") or "") == str(note_id or ""):
                return dict(item)
        return None

    def _owned_entry(self, session: Session, note_id: object) -> dict[str, Any] | None:
        if self._owned_promise(session, note_id) is not None:
            return {"id": str(note_id or ""), "kind": KIND_PROMISE}
        return self._owned_fact(session, note_id)

    # ---- 清单（note_list 工具的底） -------------------------------------------

    def note_list(self, session: Session, *, kind: object = "") -> dict[str, Any]:
        """翻本子。私聊＝关于眼前人的条目（带 id）；群聊＝只报数，不念内容。"""
        if not self._enabled():
            return {"ok": False, "error": DISABLED}
        want = str(kind or "").strip().lower()
        want = _KINDS.get(want, "") if want else ""
        if kind and not want:
            return {"ok": False, "error": "kind 只能是 fact（事实）或 promise（约定），不填＝都看。"}
        if not session.is_private:
            return {"ok": True, "scope": "group", "open_count": self._open_count()}
        who = self._person(session)
        if not who:
            return {"ok": False, "error": "这个会话取不到人，翻不了。"}
        doc = self.store.read()
        facts = [dict(item) for item in (doc.get("facts") or {}).get(who) or []]
        promises = [
            dict(item)
            for item in doc.get("promises") or []
            if str(item.get("about") or "") == who
        ]
        promises = self._by_urgency(promises, now=None)
        return {
            "ok": True,
            "scope": "private",
            "about": who,
            "about_name": self._name(who),
            "facts": facts if want in ("", KIND_FACT) else [],
            "promises": promises if want in ("", KIND_PROMISE) else [],
            "limits": dict(self._limits()),
        }

    # ---- 注入文案（compose 经 NotebookLike 调用） ------------------------------

    def promise_line(self, session: Session, *, now: datetime | None = None) -> str:
        """约定那一段：私聊出全文（过期排前）；群聊只出脱敏计数行。"""
        if not self._enabled():
            return ""
        if not session.is_private:
            count = self._open_count()
            if not count:
                return ""
            return f"（小本本：你还有 {count} 件答应过的事没办完。）"
        mine = self.promises_for(session, now=now)
        if not mine:
            return ""
        today = (now or datetime.now(self._tz())).date().isoformat()
        lines = []
        for item in mine:
            due = str(item.get("due_at") or "")
            marker = ""
            if due:
                if due < today:
                    marker = f"（已过期 {due}）"
                elif due == today:
                    marker = "（今天到期）"
                else:
                    marker = f"（{due} 前）"
            lines.append(f"- {item.get('text') or ''}{marker}")
        return "【小本本】你答应过的事：\n" + "\n".join(lines)

    def fact_line(self, session: Session, *, now: datetime | None = None) -> str:
        """事实那一段：只在与该人的私聊出；群聊一律空。"""
        facts = self.facts_for(session)
        if not facts:
            return ""
        lines = [f"- {item.get('text') or ''}" for item in facts]
        return "【小本本】关于眼前这个人，你记着：\n" + "\n".join(lines)


# ---- 小工具 --------------------------------------------------------------------


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _is_day(value: object) -> bool:
    text = str(value or "")
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    return text.replace("-", "").isdigit()


__all__ = [
    "DISABLED",
    "FULL_MESSAGE",
    "KIND_FACT",
    "KIND_PROMISE",
    "NOT_FOUND",
    "Notebook",
]
