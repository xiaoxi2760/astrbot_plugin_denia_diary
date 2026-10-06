"""主动消息门面：巡检决策（``patrol``）+ 两段式写入 + 出站素材组装。

**职责边界**：这里只决定"发不发、发什么料"——真正把消息送出去（``add_active_job``
唤醒 / ``StarTools.send_message`` 直发暗号）是 ``main.py`` 的事，core 不 import astrbot。

**先闸门后内容**（复核 #2）：``patrol`` 先判暗号（独立四道闸），再按优先级逐个问
触发器，第一个有候选的放行——一 tick 至多一条。

**两段式 ``last_sent_at``**（复核 #1）：决策时只扣配额（``today_count`` / ``last_slot``
/ ``slots_today`` / 暗号两键），``last_sent_at`` 等"真的发出去"才写——确认点在
``main.py`` 的 ``on_using_llm_tool`` 钩子（主动轮她真调了 ``send_message_to_user``）
与直发成功的返回值。中间失败（唤醒没跑、平台没找到）就是配额白吃一次，换轨迹不撒谎。

**人格一致**：唤醒轮的 ``note`` = ``compose_prompt`` 的完整注入 + ≤100 字上下文包
（类目 + 一句指令 + 素材）——主动轮和被动轮看到同一份完整注入，不另写渲染器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .. import compose, scope, storage
from ..session import Session
from . import gate, triggers
from .store import ProactiveStore

logger = logging.getLogger(__name__)

PACK_MAX = 100
"""上下文包（触发上下文）的字数上限：注入本体走 compose 的 400 预算，这里只装
「类目 + 指令 + 素材」。note 经宿主 ``json.dumps`` 进 system prompt，换行会转义成
``\\n``（每个多 1 字符）——预算按转义后算，所以 pack 里不放换行。"""

SLOT_LABELS: dict[str, str] = {
    "signal": "心情暗号",
    "promise": "约定跟进",
    "anniversary": "纪念日",
    "care": "关怀",
    "quiet": "好久没聊",
    "morning": "早安",
    "goodnight": "晚安",
    "diary": "日记",
}


@dataclass
class Proactive:
    """主动消息门面。配置、布局、文件层与四个内容门面都由调用方注入。"""

    settings: object
    layout: storage.Layout
    store: ProactiveStore
    locks: storage.KeyedLocks = field(default_factory=storage.KeyedLocks)
    diary: object = None
    notebook: object = None
    state: object = None
    affinity: object = None
    _pending_log: dict[str, dict[str, str]] = field(default_factory=dict)
    """两段式的决策暂存（umo → slot/fragment）：确认发出后转成 proactive-log 记录。

    只活在"决策 → 确认"的窗口里；重启丢失就丢了——没确认 = 没发出 = 不该记日志。"""

    # ---- 被动轮顺手做的事 ------------------------------------------------------

    async def note_contact(self, session: Session) -> None:
        """记录"这个人平时在哪跟我说话"（person_id → 会话），触发器靠它反查目标。

        挂在 ``on_llm_request``（每个请求都会走）：值没变不写盘、全程吞异常——
        它绝不能把正常对话带崩（复核 #5）。

        第 8 步加键：真拿到 ``sender_name`` 就落进 ``name``（空名字**一个字不动**——
        既不写空串也不擦已有值，换新名字才覆盖）；老 doc 没有 ``name`` 照常读。
        """
        try:
            who = session.person_id()
            if not who or not session.umo:
                return
            kind = "group" if session.is_group else "private"
            name = str(getattr(session, "sender_name", "") or "").strip()

            def update(doc: dict[str, Any]) -> dict[str, Any] | None:
                contacts = dict(doc.get("contacts") or {})
                current = contacts.get(who)
                entry = dict(current) if isinstance(current, dict) else {}
                entry["umo"] = session.umo
                entry["kind"] = kind
                if name:
                    entry["name"] = name
                if isinstance(current, dict) and current == entry:
                    return None  # 值没变（含名字没变），不写
                contacts[who] = entry
                doc["contacts"] = contacts
                return doc

            await self.store.update(update)
        except Exception as error:  # noqa: BLE001 - 记录失败不能影响对话
            logger.warning("[proactive] 联系人记录失败（已忽略）：%s", error)

    # ---- 巡检决策 ---------------------------------------------------------------

    async def patrol(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """问一圈"现在有没有一件值得说的事"。有 → 扣配额并返回出站决策；没有 → None。

        决策形状：``{"kind": "wake", "slot", "umo", "note", "fragment"}`` 或
        ``{"kind": "direct", "slot": "signal", "umo", "char", "fragment"}``。
        ``kind=wake`` 由适配器 ``add_active_job`` 唤醒她本人；``kind=direct`` 由
        适配器直发暗号字符（``send_message`` 返回 True 后调 ``confirm_sent``）。

        **先闸门后内容**（复核 #2）：免打扰是全局闸（常规内容整个静默，暗号早已
        独立放行）；配额 / 间隔是会话闸——先算出"今天还能对谁说话"，触发器只对
        这些会话取料，96 次/tick 的巡检不会白翻日记原文。
        """
        if not self.settings.subsystem("proactive"):
            return None
        moment = now or datetime.now(self.settings.zone())
        cfg = dict(self.settings.proactive)
        doc = self.store.read()

        # 先暗号：优先级最高，独立四道闸，破免打扰（决策 #16）、**穿透配额**（验收裁定：
        # 防打扰的闸门不该挡住求助信号——照扣 today_count，但不被配额挡住）
        signal = self._signal_candidate(doc, cfg, moment)
        if signal is not None:
            decision = self._build_decision(signal, cfg, moment)
            if await self._commit(decision, cfg, moment):
                self._pending_log[str(decision["umo"])] = {
                    "slot": str(decision["slot"]),
                    "fragment": str(decision["fragment"]),
                }
                return decision

        # 全局闸：免打扰时段常规内容一条都不发
        if self.state is not None and self.state.is_late_night(now=moment):
            return None

        # 会话闸：先算出"今天还能对谁说话"（配额 / 间隔；免打扰已在上面拦完）。
        # 目标宇宙由 scope.proactive_targets 给（第 7 步："谁能被主动"只住在 scope，
        # 跟着档位走；contacts 是唯一来源——没互动过的人 / 没见过的群进不来）。
        allowed = self._allowed_sessions(doc, cfg, moment, mode=self._scope_mode())

        # 再常规触发器：按优先级逐个问，第一个有内容的放行
        for finder in triggers.FINDERS:
            candidates = finder(self, doc, now=moment, allowed=allowed)
            if not candidates:
                continue
            decision = self._build_decision(candidates[0], cfg, moment)
            if await self._commit(decision, cfg, moment):
                self._pending_log[str(decision["umo"])] = {
                    "slot": str(decision["slot"]),
                    "fragment": str(decision["fragment"]),
                }
                return decision
            return None  # 闸门复核没过（竞态）：本 tick 放弃，不往下问
        return None

    def _scope_mode(self) -> str:
        """当前启用范围档位（未配置时由 scope 按默认档兜底）。"""
        return str(dict(self.settings.scope).get("mode") or "")

    def _allowed_sessions(
        self, doc: dict[str, Any], cfg: dict[str, Any], moment: datetime, *, mode: str = ""
    ) -> dict[str, str]:
        """过会话闸（配额 / 最小间隔）的目标：``umo → kind``。

        目标宇宙 = ``scope.proactive_targets``（档位决定"谁能被主动"，contacts 是
        唯一来源）；这里只逐会话复核配额与间隔（``late_night=False``：免打扰已在
        patrol 的全局闸拦完，不在这里重复判）。
        """
        contacts = doc.get("contacts")
        contacts = contacts if isinstance(contacts, dict) else {}
        known_groups: list[str] = []
        seen_groups: set[str] = set()
        for contact in contacts.values():
            if not isinstance(contact, dict) or str(contact.get("kind") or "") != "group":
                continue
            umo = str(contact.get("umo") or "")
            if umo and umo not in seen_groups:
                seen_groups.add(umo)
                known_groups.append(umo)
        sessions = doc.get("sessions")
        targets = scope.proactive_targets(
            mode,
            contacts=contacts,
            love_peers=tuple(self.settings.love_peers or ()),
            sessions=sessions if isinstance(sessions, dict) else {},
            known_groups=known_groups,
        )
        allowed: dict[str, str] = {}
        for umo, kind in targets:
            reason = gate.gate_check(
                entry=gate.entry_of(doc, umo),
                kind=kind,
                cfg=cfg,
                late_night=False,
                now=moment,
            )
            if reason is None:
                allowed[umo] = kind
        return allowed

    async def confirm_sent(self, umo: str, *, now: datetime | None = None) -> bool:
        """两段式第二步：确认"真的发出去了"，写 ``last_sent_at`` 并追加发送记录。

        发送记录（``proactive-log.jsonl``，``{ts, umo, slot, fragment}``）是 WebUI
        「主动消息记录」的唯一数据源，**只在确认发出后**追加；slot / fragment 取
        决策时的暂存，重启丢失就取 ``last_slot``（fragment 记空串——行还在，
        "她什么时候找过谁"不丢）。

        **暗号的冷却也在这一步写**（第 11 步改）：``signal_date``（每日一次）与
        ``signal_last_at``（独立冷却）都记在**真的发出去之后**。改之前它们记在
        ``_commit``（决策时）—— 发送失败不回滚，于是"一次没送达 = 她 3 天不能再用暗号"。
        """
        if not umo:
            return False
        moment = now or datetime.now(self.settings.zone())
        pending = self._pending_log.pop(str(umo), None)
        # slot 先算出来：下面"要不要补记暗号冷却"和发送记录都要它。
        slot = str((pending or {}).get("slot") or "") or str(
            gate.entry_of(self.store.read(), umo).get("last_slot") or ""
        )

        def update(doc: dict[str, Any]) -> dict[str, Any]:
            sessions = dict(doc.get("sessions") or {})
            entry = dict(sessions.get(umo) or {})
            entry["last_sent_at"] = moment.isoformat(timespec="seconds")
            if slot == "signal":
                entry["signal_date"] = moment.date().isoformat()
                entry["signal_last_at"] = moment.isoformat(timespec="seconds")
            sessions[umo] = entry
            doc["sessions"] = sessions
            return doc

        await self.store.update(update)
        fragment = str((pending or {}).get("fragment") or "")
        await self.store.append_log(
            [
                {
                    "ts": moment.isoformat(timespec="seconds"),
                    "umo": umo,
                    "slot": slot,
                    "fragment": fragment,
                }
            ],
            now=moment,
        )
        return True

    # ---- 内部：暗号候选 ----------------------------------------------------------

    def _signal_candidate(
        self, doc: dict[str, Any], cfg: dict[str, Any], moment: datetime
    ) -> triggers.Candidate | None:
        peers = tuple(self.settings.love_peers or ())
        if not peers:
            return None
        contacts = doc.get("contacts")
        contacts = contacts if isinstance(contacts, dict) else {}
        # 当下坐标是全局一份（情绪不按人分）；判衰减后的值——语义是"她现在还在难受"
        snapshot = self.state.snapshot(now=moment) if self.state is not None else {}
        valence = (snapshot.get("mood") or {}).get("valence")
        char = str(cfg.get("signal_char") or "。")
        for pid in peers:
            contact = contacts.get(pid)
            if not isinstance(contact, dict) or str(contact.get("kind") or "") != "private":
                continue  # 暗号绝不进群
            umo = str(contact.get("umo") or "")
            if not umo:
                continue
            entry = gate.entry_of(doc, umo)
            reason = gate.signal_gate(
                person_id=pid,
                love_peers=peers,
                snapshot_valence=valence,
                entry=entry,
                cfg=cfg,
                now=moment,
            )
            if reason is None:
                return triggers.Candidate(
                    slot="signal",
                    umo=umo,
                    session=Session.from_umo(umo),
                    fragment=f"暗号字符「{char}」",
                    instruction="整条消息就是那个字符本身，不带任何解释",
                    priority=triggers.PRIORITY_ORDER.index("signal"),
                )
        return None

    # ---- 内部：出站决策与配额 -----------------------------------------------------

    def _build_decision(
        self, candidate: triggers.Candidate, cfg: dict[str, Any], moment: datetime
    ) -> dict[str, Any]:
        if candidate.slot == "signal":
            return {
                "kind": "direct",
                "slot": candidate.slot,
                "umo": candidate.umo,
                "char": str(cfg.get("signal_char") or "。"),
                "fragment": candidate.fragment,
            }
        return {
            "kind": "wake",
            "slot": candidate.slot,
            "umo": candidate.umo,
            "note": self._build_note(candidate, now=moment),
            "fragment": candidate.fragment,
        }

    def _build_note(self, candidate: triggers.Candidate, *, now: datetime) -> str:
        """完整注入（复用第 3 步渲染器）+ ≤100 字上下文包。

        群聊目标**不许裸发**（第 7 步护栏）：上下文包开头就要求她先 @ 提及对方。
        """
        inject = ""
        if self.diary is not None:
            inject = compose.compose_prompt(
                self.diary,
                candidate.session,
                notebook=self.notebook,
                state=self.state,
                affinity=self.affinity,
                now=now,
            )
        label = SLOT_LABELS.get(candidate.slot, candidate.slot)
        at_rule = "" if candidate.session.is_private else "（群聊：开口必须先 @ TA，别裸发）"
        pack = f"【{label}】{at_rule}{candidate.instruction}素材：{candidate.fragment}"
        pack = " ".join(pack.split())[:PACK_MAX]  # 压掉换行（json.dumps 转义预算）+ 截 100
        return f"{inject}\n{pack}" if inject else pack

    async def _commit(
        self, decision: dict[str, Any], cfg: dict[str, Any], moment: datetime
    ) -> bool:
        """两段式第一步：扣配额（决策时），**不写** ``last_sent_at``，
        也**不写**暗号的 ``signal_date`` / ``signal_last_at``（第 11 步改，见 ``confirm_sent``）。

        锁内复核配额（防巡检重叠），返回是否真的扣成了。**暗号跳过配额复核**
        （验收裁定：穿透配额，与"破免打扰"同理）——但 `today_count` 照常自增
        （暗号也是一条主动消息，计数不失真）。
        """
        umo = str(decision["umo"])
        slot = str(decision["slot"])
        today = moment.date().isoformat()

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            entry = gate.entry_of(doc, umo)
            kind = "group" if (Session.from_umo(umo).is_group) else "private"
            limit = int(cfg.get("daily_limit_group" if kind == "group" else "daily_limit_private") or 0)
            if slot != "signal" and gate.today_count(entry, today) >= limit:
                return None  # 配额刚满（竞态）：放弃，什么都不写（暗号不在此列）
            sessions = dict(doc.get("sessions") or {})
            entry = dict(entry)
            entry["today_date"] = today
            entry["today_count"] = gate.today_count(entry, today) + 1
            entry["last_slot"] = slot
            slots = dict(entry.get("slots_today") or {})
            today_slots = list(slots.get(today) or [])
            if slot not in today_slots:
                today_slots.append(slot)
            slots = {day: value for day, value in slots.items() if day == today}
            slots[today] = today_slots
            entry["slots_today"] = slots
            # ⚠️ 暗号的 `signal_date` / `signal_last_at` **不在这里写**（第 11 步改）：
            # 这一层是"预留"，只负责防重复（配额 / slots_today / last_slot）。
            # 冷却不是防重复的手段，它是**送出过**的记账 —— 放到 confirm_sent 里，
            # 真发出去了才写。否则一次发送失败（平台没送达）会白烧 3 天冷却，
            # 而她本人和用户都不知道为什么。
            sessions[umo] = entry
            doc["sessions"] = sessions
            return doc

        result = await self.store.update(update)
        return result is not None


__all__ = ["Proactive", "PACK_MAX", "SLOT_LABELS"]
