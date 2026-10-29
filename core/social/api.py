"""群社交门面：登记（群/参与者）、有界话题、bot 短聊闸门、面板数据。

职责边界（与既有系统对齐）：

- **观察 ≠ 回复**。``observe_group`` 只做记录（待登记 / 话题 / 参与者补录），
  绝不请求模型；``chat_decision`` 只对"已登记 bot + 允许互动 + 叫了她"的消息
  产出唤醒决定，唤醒走 ``main.py`` 既有的 ``add_active_job`` 通道——不另起
  发送路径，回执沿用 ``confirm_sent`` 的两段式；
- **范围外整轮不介入**（``core.scope`` 唯一判定）：``scope.mode`` 不是 ``all``
  时，群消息一条都不观察、不落盘；
- **私聊数据不进群素材**：本门面的素材来源只有群消息原文与 ``Life`` 的公开
  日程，从不读日记 / 小本本 / 留言 / 情绪——有测试钉住。

配置全部来自 ``settings.social``（``core.settings`` 校验后的快照），越界值在
加载层已夹紧；这里不再二次解释配置。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import scope
from .._log import logger
from . import chat, registry, topics
from .store import DECISIONS_KEEP, SocialStore

TRACE_BUDGET = 60
"""群话题注入行的自身预算（trace 档 70 字的一部分，给熟悉度/问候留余量）。"""

PARTICIPANTS_MAX = 200
"""单个群的参与者登记上限（防手滑把文件写爆；正常群远到不了）。"""


class Social:
    """群社交门面。settings/layout/locks 由调用方注入；``life`` 只用它的公开快照。"""

    def __init__(
        self,
        settings: Any,
        layout: Any,
        locks: Any = None,
        life: Any = None,
    ) -> None:
        self.settings = settings
        self.layout = layout
        self.store = SocialStore(layout=layout, locks=locks)
        self.life = life

    # ---- 配置速读 --------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        raw = getattr(self.settings, "social", None)
        return dict(raw) if isinstance(raw, dict) else {}

    def _self_names(self) -> tuple[str, ...]:
        return chat.parse_self_names(self._cfg().get("self_names"))

    def enabled(self) -> bool:
        return bool(self.settings.subsystem("social"))

    # ---- 观察入口（被动、零模型调用） -------------------------------------------

    def scope_reason(self, session: Any) -> str:
        """这个会话在不在启用范围（群聊只有 ``all`` 档放行）。空串＝放行。"""
        allowed, reason = scope.allows(
            str(dict(self.settings.scope).get("mode") or ""),
            is_private=False,
            person_id=session.person_id(),
            love_peers=list(self.settings.love_peers),
        )
        return "" if allowed else reason

    async def observe_group(self, session: Any, *, text: str, now: datetime) -> dict[str, Any]:
        """记录一条群消息（不回复、不调用模型）。返回 ``{"observed": bool, "reason": str}``。

        - 范围外（``scope.mode`` 非 all）：整轮不介入，一个字节不写；
        - 没登记的群：只记一条"最近看到"（有界），供主人在面板一键登记；
        - 登记但没启用的群：什么都不记（关掉的群连话题都不攒）；
        - 启用的群：话题按容量/TTL/去重落一条；陌生说话人补一条参与者登记
          （默认按"人"记、不允许互动——是 bot 要主人在面板标注）。
        """
        umo = str(getattr(session, "umo", "") or "")
        if not self.enabled():
            return {"observed": False, "reason": "群社交未启用"}
        reason = self.scope_reason(session)
        if reason:
            return {"observed": False, "reason": reason}
        if not umo:
            return {"observed": False, "reason": "会话没有 umo"}

        cfg = self._cfg()
        doc = self.store.read()
        if not registry.group_enabled(doc, umo):
            if umo not in self.store.section(doc, "groups"):
                await self._record_seen(session, now=now)
                return {"observed": False, "reason": "群未登记（已记入待登记）"}
            return {"observed": False, "reason": "群已登记但未启用"}

        if not cfg.get("topic_enabled", True):
            if not self._needs_participant(doc, umo, session):
                return {"observed": False, "reason": "群话题收集已关闭"}

            def add_participant(current: dict[str, Any]) -> dict[str, Any] | None:
                if not self._needs_participant(current, umo, session):
                    return None
                self._auto_participant(current, umo, session, now=now)
                return self.store.truncate_doc(
                    current, seen_max=_cfg_int(cfg, "seen_max", 30)
                )

            await self.store.update(add_participant)
            return {"observed": True, "reason": "群话题收集已关闭，仅补录参与者"}

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            items = self.store.section(doc, "topics").get(umo)
            items = topics.fresh_items(
                items, now=now, ttl_hours=_cfg_int(cfg, "topic_ttl_hours", 24)
            )
            recorded = topics.record_message(
                items,
                ts=now.isoformat(timespec="seconds"),
                speaker_id=str(session.person_id() or ""),
                speaker_name=str(getattr(session, "sender_name", "") or ""),
                text=str(text or ""),
                max_items=_cfg_int(cfg, "topic_max_items", 12),
                max_chars=_cfg_int(cfg, "topic_max_chars", 120),
            )
            if recorded == list(items or []) and not self._needs_participant(doc, umo, session):
                return None  # 刷屏重复 + 老面孔：一次写盘都省掉
            all_topics = dict(self.store.section(doc, "topics"))
            all_topics[umo] = recorded
            doc["topics"] = all_topics
            self._auto_participant(doc, umo, session, now=now)
            return self.store.truncate_doc(doc, seen_max=_cfg_int(cfg, "seen_max", 30))

        await self.store.update(update)
        return {"observed": True, "reason": ""}

    def _needs_participant(self, doc: dict[str, Any], umo: str, session: Any) -> bool:
        pid = str(session.person_id() or "")
        return bool(pid) and registry.participant_of(doc, umo, pid) is None

    def _auto_participant(self, doc: dict[str, Any], umo: str, session: Any, *, now: datetime) -> None:
        """陌生说话人补一条参与者登记（按"人"记、不允许互动；主人在面板改）。"""
        pid = str(session.person_id() or "")
        if not pid or registry.participant_of(doc, umo, pid) is not None:
            return
        record, reason = registry.normalize_participant(
            {"id": pid, "name": str(getattr(session, "sender_name", "") or "")},
            now=now.isoformat(timespec="seconds"),
        )
        if record is None:
            return
        all_participants = dict(self.store.section(doc, "participants"))
        group = dict(all_participants.get(umo) or {})
        if len(group) >= PARTICIPANTS_MAX:
            return
        group[pid] = record
        all_participants[umo] = group
        doc["participants"] = all_participants

    async def _record_seen(self, session: Any, *, now: datetime) -> None:
        """没登记的群记一条"最近看到"（有界，最旧的先挤掉）。"""
        umo = str(getattr(session, "umo", "") or "")
        entry = {
            "platform": str(getattr(session, "platform", "") or ""),
            "group_id": str(getattr(session, "group_id", "") or ""),
            "name": str(getattr(session, "group_name", "") or "")[: registry.MAX_NAME],
            "last_seen_at": now.isoformat(timespec="seconds"),
        }

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            seen = dict(self.store.section(doc, "seen"))
            current = seen.get(umo)
            if isinstance(current, dict):
                entry["name"] = entry["name"] or str(current.get("name") or "")
                if entry == current:
                    return None  # 同一条重复刷：不写
            seen[umo] = entry
            doc["seen"] = seen
            cfg = self._cfg()
            return self.store.truncate_doc(doc, seen_max=_cfg_int(cfg, "seen_max", 30))

        try:
            await self.store.update(update)
        except Exception as error:  # noqa: BLE001 - 观察失败只留日志
            logger.warning("[social] 记录待登记群失败（已忽略）：%s", error)

    # ---- bot 短聊闸门 ------------------------------------------------------------

    def _chat_preflight(self, session: Any) -> chat.ChatDecision | None:
        """不用碰盘就能拦下的判定（开关 / 范围 / 群未启用）。``None``＝继续细判。"""
        if not self.enabled():
            return chat.ChatDecision(action="ignore", reason="群社交未启用")
        reason = self.scope_reason(session)
        if reason:
            return chat.ChatDecision(action="ignore", reason=reason)
        umo = str(getattr(session, "umo", "") or "")
        if not umo:
            return chat.ChatDecision(action="ignore", reason="会话没有 umo")
        if not registry.group_enabled(self.store.read(), umo):
            return chat.ChatDecision(action="ignore", reason="群未登记或未启用")
        return None

    def manages_message(self, session: Any, text: str) -> bool:
        """这条群消息是否由社交闸门负责（纯只读，不消耗轮数或指纹）。

        适配器用它决定是否接管宿主事件：只要是启用范围内、已登记启用群中
        获准互动的 bot 叫了她，就由本闸门负责，即使后续因免打扰、轮数、去重
        或主动配额而拒绝唤醒，也不能让宿主再为同一条 @ 独立回复。
        """
        if session is None or not getattr(session, "is_group", False):
            return False
        if not self.enabled() or self.scope_reason(session):
            return False
        umo = str(getattr(session, "umo", "") or "")
        pid = str(session.person_id() or "")
        if not umo or not pid:
            return False
        doc = self.store.read()
        if not registry.group_enabled(doc, umo):
            return False
        participant = registry.participant_of(doc, umo, pid)
        if not isinstance(participant, dict):
            return False
        if not participant.get("is_bot") or not participant.get("allow_interaction"):
            return False
        body = " ".join(str(text or "").split())
        return any(name in body for name in self._self_names())

    async def chat_decision(  # noqa: C901 - 单入口判定，闸门顺序不可拆
        self, session: Any, *, text: str, now: datetime, late_night: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """判定"这条群消息要不要让她回一句"。

        Only "已登记 bot + 允许互动 + 叫了她"的消息才会改状态（轮数 / 指纹）；
        已获准 bot 的拒绝原因只进有界决策日志，不记拒绝消息原文。
        """
        quick = self._chat_preflight(session)
        if quick is not None:
            return _decision_payload(quick)
        umo = str(getattr(session, "umo", "") or "")
        cfg = self._cfg()
        pid = str(session.person_id() or "")
        holder: dict[str, chat.ChatDecision] = {}

        def apply(doc: dict[str, Any]) -> dict[str, Any] | None:
            chats = dict(self.store.section(doc, "chats"))
            state = chats.get(umo)
            participant = registry.participant_of(doc, umo, pid)
            new_state, decided = chat.evaluate(
                state,
                bot_id=pid,
                bot_name=str(getattr(session, "sender_name", "") or "")
                or str((participant or {}).get("name") or ""),
                text=str(text or ""),
                participant=participant,
                self_names=self._self_names(),
                max_rounds=_cfg_int(cfg, "max_rounds", 3),
                session_window_minutes=_cfg_int(cfg, "session_window_minutes", 10),
                session_cooldown_minutes=_cfg_int(cfg, "session_cooldown_minutes", 30),
                now=now,
                late_night=bool(late_night),
            )
            holder["decision"] = decided
            if dry_run:
                return None
            baseline = state if isinstance(state, dict) else chat.empty_chat_state()
            can_log = participant is not None and participant.get("allow_interaction")
            if new_state == baseline:
                if can_log and decided.action == "ignore":
                    self._append_decision(
                        doc,
                        umo=umo,
                        action=decided.action,
                        reason=decided.reason,
                        slot="group_chat",
                        fragment="",
                        round=decided.round,
                        now=now,
                    )
                    return doc
                return None  # 没过闸的消息不改会话状态；拒绝原因单独有界记录
            chats[umo] = new_state
            doc["chats"] = chats
            if can_log:
                self._append_decision(
                    doc,
                    umo=umo,
                    action=decided.action,
                    reason=decided.reason,
                    slot="group_chat",
                    fragment=decided.fragment if decided.action == "wake" else "",
                    round=decided.round,
                    now=now,
                )
            return doc

        try:
            await self.store.update(apply)
        except Exception as error:  # noqa: BLE001 - 判定失败绝不能带崩消息处理
            logger.warning("[social] 短聊判定失败（按忽略处理）：%s", error)
            holder.setdefault("decision", chat.ChatDecision(action="ignore", reason="判定失败，已忽略"))
        return _decision_payload(holder.get("decision") or chat.ChatDecision(action="ignore"))

    def _append_decision(
        self,
        doc: dict[str, Any],
        *,
        umo: str,
        action: str,
        reason: str,
        slot: str,
        fragment: str,
        round: int,
        now: datetime,
    ) -> None:
        """往决策记录（有界）追加一条；只在已持锁的 update 闭包里调用。"""
        decisions = doc.get("decisions")
        decisions = decisions if isinstance(decisions, list) else []
        decisions.append(
            {
                "ts": now.isoformat(timespec="seconds"),
                "umo": umo,
                "action": action,
                "reason": str(reason or "")[:160],
                "slot": str(slot or ""),
                "fragment": str(fragment or "")[:80],
                "round": int(round or 0),
            }
        )
        doc["decisions"] = decisions[-DECISIONS_KEEP:]

    async def record_decision(
        self,
        session: Any,
        *,
        action: str,
        reason: str,
        slot: str = "group_chat",
        round: int = 0,
        now: datetime,
    ) -> bool:
        """记录一条不含消息正文的社交决定，供面板解释 proactive 闸门拒绝。"""
        if session is None or not getattr(session, "is_group", False):
            return False
        umo = str(getattr(session, "umo", "") or "")
        if not umo:
            return False

        def update(doc: dict[str, Any]) -> dict[str, Any]:
            self._append_decision(
                doc,
                umo=umo,
                action=str(action or "ignore"),
                reason=str(reason or ""),
                slot=str(slot or "group_chat"),
                fragment="",
                round=max(0, int(round or 0)),
                now=now,
            )
            return doc

        try:
            await self.store.update(update)
            return True
        except Exception as error:  # noqa: BLE001 - 面板记录失败不能影响消息管线
            logger.warning("[social] 记录短聊决定失败（已忽略）：%s", error)
            return False

    # ---- 巡检候选素材（给 core/proactive 的群触发器用） ---------------------------

    def topic_material(self, umo: str, *, now: datetime) -> dict[str, Any] | None:
        """"接着近期公开话题说几句"的素材。群没启用 / 没有新鲜话题 → None。"""
        if not self.enabled():
            return None
        cfg = self._cfg()
        if not cfg.get("topic_enabled", True):
            return None
        doc = self.store.read()
        if not registry.group_enabled(doc, umo):
            return None
        items = topics.fresh_items(
            self.store.section(doc, "topics").get(umo),
            now=now,
            ttl_hours=_cfg_int(cfg, "topic_ttl_hours", 24),
        )
        material = topics.material_from(items)
        if material is None:
            return None
        clock = f"{material['clock']} " if material.get("clock") else ""
        who = material.get("speaker_name") or "有人"
        return {
            "fragment": f"{clock}{who}说「{material['text']}」",
            "instruction": "群里刚刚在聊这件事，自然地接一句你自己的看法，别复述原文",
            "speaker_id": material.get("speaker_id") or "",
        }

    def share_material(self, umo: str, *, now: datetime) -> dict[str, Any] | None:
        """"分享当前日程活动"的素材（公开角色生活，允许进群）。没有进行中的活动 → None。"""
        if not self.enabled():
            return None
        if not registry.group_enabled(self.store.read(), umo):
            return None
        if self.life is None:
            return None
        try:
            snapshot = self.life.snapshot(now=now)
        except Exception:  # noqa: BLE001 - 日程拿不到就不出这个候选
            return None
        if not isinstance(snapshot, dict) or not snapshot.get("enabled", True):
            return None
        current = snapshot.get("current")
        if not isinstance(current, dict):
            return None
        title = str(current.get("title") or "").strip()
        if not title:
            return None
        where = str(current.get("location") or "").strip()
        fragment = f"此刻在做：{title}" + (f"（在{where}）" if where else "")
        return {
            "fragment": fragment,
            "instruction": "自然地分享一句你正在做的事，像顺口一提，别像汇报打卡",
        }

    def enabled_group_umos(self) -> list[str]:
        """已登记且启用的群（给主动消息目标集合做扩展；scope 仍只认 ``all`` 档）。"""
        if not self.enabled():
            return []
        return registry.enabled_group_umos(self.store.read())

    # ---- 注入（被动轮，走 compose 的 trace 档） -----------------------------------

    def trace_line(self, session: Any, *, now: datetime | None = None) -> str:
        """群话题注入行。**私聊恒为空串**——群素材绝不进私聊提示词。"""
        if session is None or not getattr(session, "is_group", False):
            return ""
        if not self.enabled():
            return ""
        cfg = self._cfg()
        if not cfg.get("topic_enabled", True):
            return ""
        moment = now or datetime.now(self.settings.zone())
        umo = str(getattr(session, "umo", "") or "")
        doc = self.store.read()
        if not registry.group_enabled(doc, umo):
            return ""
        items = topics.fresh_items(
            self.store.section(doc, "topics").get(umo),
            now=moment,
            ttl_hours=_cfg_int(cfg, "topic_ttl_hours", 24),
        )
        return topics.trace_line_text(items, now=moment, budget=TRACE_BUDGET)

    # ---- 面板数据与登记 CRUD ------------------------------------------------------

    def panel_payload(self, *, now: datetime) -> dict[str, Any]:
        """GET social 的数据：登记表、待登记、话题概览、最近决策、生效配置。"""
        cfg = self._cfg()
        doc = self.store.read()
        groups = self.store.section(doc, "groups")
        participants = self.store.section(doc, "participants")
        all_topics = self.store.section(doc, "topics")
        out_groups: list[dict[str, Any]] = []
        for umo, group in groups.items():
            if not isinstance(group, dict):
                continue
            fresh = topics.fresh_items(
                all_topics.get(umo), now=now, ttl_hours=_cfg_int(cfg, "topic_ttl_hours", 24)
            )
            out_groups.append(
                {
                    **group,
                    "participants": [
                        {"id": pid, **(entry if isinstance(entry, dict) else {})}
                        for pid, entry in (participants.get(umo) or {}).items()
                        if isinstance(entry, dict)
                    ],
                    "topics": fresh[-6:],
                    "topic_total": len(fresh),
                    "chat": self.store.section(doc, "chats").get(umo) or {},
                }
            )
        seen = self.store.section(doc, "seen")
        return {
            "ok": True,
            "enabled": self.enabled(),
            "scope_mode": str(dict(self.settings.scope).get("mode") or ""),
            "self_names": list(self._self_names()),
            "config": {
                "topic_enabled": bool(cfg.get("topic_enabled", True)),
                "topic_max_items": _cfg_int(cfg, "topic_max_items", 12),
                "topic_ttl_hours": _cfg_int(cfg, "topic_ttl_hours", 24),
                "max_rounds": _cfg_int(cfg, "max_rounds", 3),
                "session_window_minutes": _cfg_int(cfg, "session_window_minutes", 10),
                "session_cooldown_minutes": _cfg_int(cfg, "session_cooldown_minutes", 30),
            },
            "groups": out_groups,
            "seen": [
                {"umo": umo, **(entry if isinstance(entry, dict) else {})}
                for umo, entry in sorted(
                    seen.items(),
                    key=lambda item: str((item[1] or {}).get("last_seen_at") or ""),
                    reverse=True,
                )
            ],
            "decisions": list(doc.get("decisions") or [])[-20:],
        }

    async def save_group(self, body: Any, *, now: datetime) -> dict[str, Any]:
        """登记/更新一个群（面板入口）。新群默认 ``enabled=False``。

        拒绝原因用 holder 带回（HTTP 错误响应直接给主人看），**不**为一次被拒的
        编辑写盘——决策日志只记短聊，不记面板操作。
        """
        cfg = self._cfg()
        stamp = now.isoformat(timespec="seconds")
        error: list[str] = []

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            groups = dict(self.store.section(doc, "groups"))
            umo = str((body or {}).get("umo") or "")
            existing = groups.get(umo) if umo else None
            record, reason = registry.normalize_group(body, now=stamp, existing=existing)
            if record is None:
                error.append(reason)
                return None
            if existing is None and len(groups) >= _cfg_int(cfg, "max_groups", 20):
                error.append(f"登记群数已达上限（{_cfg_int(cfg, 'max_groups', 20)}）")
                return None
            groups[record["umo"]] = record
            doc["groups"] = groups
            return self.store.truncate_doc(doc, seen_max=_cfg_int(cfg, "seen_max", 30))

        result = await self.store.update(update)
        if result is None:
            return {"ok": False, "error": error[0] if error else "没有改成"}
        return {"ok": True}

    async def delete_group(self, umo: str, *, now: datetime) -> dict[str, Any]:
        """删除登记：连它的参与者、话题、短聊状态一起清（数据最小化）。"""
        umo = str(umo or "").strip()

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            groups = dict(self.store.section(doc, "groups"))
            if umo not in groups:
                return None
            groups.pop(umo, None)
            doc["groups"] = groups
            participants = dict(self.store.section(doc, "participants"))
            participants.pop(umo, None)
            doc["participants"] = participants
            all_topics = dict(self.store.section(doc, "topics"))
            all_topics.pop(umo, None)
            doc["topics"] = all_topics
            chats = dict(self.store.section(doc, "chats"))
            chats.pop(umo, None)
            doc["chats"] = chats
            return doc

        result = await self.store.update(update)
        if result is None:
            return {"ok": False, "error": "没有登记这个群"}
        return {"ok": True}

    async def save_participant(self, umo: str, body: Any, *, now: datetime) -> dict[str, Any]:
        """登记/更新一个参与者（真实 id；人/bot、是否允许互动由主人标注）。

        拒绝原因用 holder 带回（同 ``save_group``），不为被拒的编辑写盘。
        """
        umo = str(umo or "").strip()
        stamp = now.isoformat(timespec="seconds")
        error: list[str] = []

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            groups = self.store.section(doc, "groups")
            if umo not in groups:
                error.append("先登记群，再登记参与者")
                return None
            participants = dict(self.store.section(doc, "participants"))
            group = dict(participants.get(umo) or {})
            pid = str((body or {}).get("id") or (body or {}).get("person_id") or "")
            existing = group.get(pid) if pid else None
            record, reason = registry.normalize_participant(body, now=stamp, existing=existing)
            if record is None:
                error.append(reason)
                return None
            if existing is None and len(group) >= PARTICIPANTS_MAX:
                error.append(f"这个群的参与者登记已达上限（{PARTICIPANTS_MAX}）")
                return None
            group[record["id"]] = record
            participants[umo] = group
            doc["participants"] = participants
            return doc

        result = await self.store.update(update)
        if result is None:
            return {"ok": False, "error": error[0] if error else "没有改成"}
        return {"ok": True}

    async def delete_participant(self, umo: str, pid: str, *, now: datetime) -> dict[str, Any]:
        umo = str(umo or "").strip()
        pid = str(pid or "").strip()

        def update(doc: dict[str, Any]) -> dict[str, Any] | None:
            participants = dict(self.store.section(doc, "participants"))
            group = dict(participants.get(umo) or {})
            if pid not in group:
                return None
            group.pop(pid, None)
            participants[umo] = group
            doc["participants"] = participants
            return doc

        result = await self.store.update(update)
        if result is None:
            return {"ok": False, "error": "这个群没有这个参与者"}
        return {"ok": True}


def _cfg_int(cfg: dict[str, Any], key: str, default: int) -> int:
    """读整数配置：**只**在缺键/None 时回落默认——0 是合法值（冷却 0 分钟、
    待登记 0 条都是主人显式选的），不能拿 ``or`` 一刀切。"""
    value = cfg.get(key)
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def _decision_payload(decision: chat.ChatDecision) -> dict[str, Any]:
    return {
        "action": decision.action,
        "reason": decision.reason,
        "round": decision.round,
        "fragment": decision.fragment,
        "bot_id": decision.bot_id,
        "bot_name": decision.bot_name,
        "trail": [{"gate": name, "note": note} for name, note in decision.trail],
    }


__all__ = ["PARTICIPANTS_MAX", "Social"]
