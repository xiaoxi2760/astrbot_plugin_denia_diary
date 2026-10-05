"""会话身份（第 1 步）。

把平台事件翻译成"按人 / 按本"的索引。**core 不 import astrbot**：``from_event`` 用鸭子类型
读属性，适配器保证事件形状，因此内核可以完全离线测试。

同一个 ``Session`` 承担四件事：① 日记标注写给谁；② 恋爱日记分流；③ 提醒冷却的粒度；
④ 恋爱日记的可见性。
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP = "group"
PRIVATE = "private"
UNKNOWN = ""
CRON_SENDER_NAME = "Scheduler"
"""主动唤醒事件的固定昵称（上游 ``core/cron/events.py:24``，别跟着改）。"""


@dataclass(frozen=True)
class Session:
    """一个会话。字段全部可选，填不出就是空字符串（降级而不是报错）。"""

    umo: str = ""
    platform: str = ""
    message_type: str = ""
    session_id: str = ""
    group_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    group_name: str = ""

    @property
    def is_group(self) -> bool:
        if self.message_type:
            return self.message_type.lower() == "groupmessage"
        return bool(self.group_id)

    @property
    def is_private(self) -> bool:
        return not self.is_group

    @property
    def kind(self) -> str:
        return GROUP if self.is_group else PRIVATE

    @classmethod
    def from_umo(cls, umo: str) -> Session:
        """``platform:message_type:session_id`` → Session（只需它的三个字段时用）。"""
        text = str(umo or "")
        parts = text.split(":")
        return cls(
            umo=text,
            platform=parts[0] if len(parts) > 0 else "",
            message_type=parts[1] if len(parts) > 1 else "",
            session_id=parts[2] if len(parts) > 2 else "",
        )

    @classmethod
    def from_event(cls, event: object) -> Session:
        """从 AstrBot 的 ``AstrMessageEvent`` 取身份（鸭子类型，不 import astrbot）。

        主动轮（cron 唤醒）的事件 **sender 是合成的**：上游 ``CronMessageEvent``
        把 ``sender.user_id`` 设成 ``session.session_id``、``nickname`` 固定成
        ``"Scheduler"``（``core/cron/events.py:23-24``、``39``）。认不出这一点，
        日记头行就会写成 ``〔和Scheduler〕``——真机 2026-10-05 10:00 那段就是这么来的。
        """
        umo = _text(getattr(event, "unified_msg_origin", ""))
        base = cls.from_umo(umo)
        message = getattr(event, "message_obj", None)
        sender = getattr(message, "sender", None)
        group = getattr(message, "group", None)
        sender_id = _call(event, "get_sender_id") or _text(getattr(sender, "user_id", ""))
        sender_name = _call(event, "get_sender_name") or _text(getattr(sender, "nickname", ""))
        if _is_synthetic_sender(event, base, sender_id, sender_name):
            sender_name = ""  # 交给 ``label()`` 退回 id；要人话就配 name_preference
        return cls(
            umo=umo,
            platform=base.platform,
            message_type=base.message_type or _text(getattr(message, "type", "")),
            session_id=base.session_id or _text(getattr(message, "session_id", "")),
            group_id=_call(event, "get_group_id") or _text(getattr(message, "group_id", "")),
            sender_id=sender_id,
            sender_name=sender_name,
            group_name=_text(getattr(group, "group_name", "")),
        )

    def peer_id(self) -> str:
        """私聊对端的 id（群聊返回空串）。"""
        return self.sender_id if self.is_private else ""

    def person_id(self) -> str:
        """"眼前这个人"的 id：私聊 = 对端，群聊 = 发言者。

        与 ``peer_id()`` 的区别：群聊不再返回空串——小本本记事、熟悉度、
        主动消息都以"当前对话里的这个人"为索引，判据收口在这一处。
        ``sender_id`` 取不到时（平台降级，或主动轮 cron 唤醒只有 umo、
        ``Session.from_umo`` 不填 sender）从 ``session_id`` 兜底推导；
        群聊的 session_id 是群号不是人，所以只对私聊兜底。
        """
        return self.sender_id or (self.session_id if self.is_private else "")

    def label(self, name_preference: dict[str, str] | None = None) -> str:
        """落盘标注：私聊 ``和某某``，群聊 ``群·群名``。

        这是"显式 scope 标记"（v0.3 的决定）：群聊一律带 ``群·`` 前缀，
        这样读取侧不必靠"以『和』开头"猜（原包会把"和平精英交流群"误判成私聊）。

        私聊取名字的顺序是 ``name_preference[person_id]`` → ``sender_name`` → id；
        **sender_id 为空时不信 sender_name**——主动轮/定时唤醒的事件没有真实发送者，
        平台会塞一个合成名字（真机实测是 ``Scheduler``），那不是一个"人"的名字，
        写进本子既会泄露内部名、又和 ``person_id()`` 的索引对不上。
        """
        from .diary import format as fmt

        pref = name_preference or {}
        if self.is_private:
            pid = self.person_id()
            who = pref.get(pid) or (self.sender_name if self.sender_id else "") or pid
            return f"和{who}" if who else ""
        who = pref.get(self.group_id) or self.group_name or (f"群{self.group_id}" if self.group_id else "")
        return f"{fmt.GROUP_TAG_PREFIX}{who}" if who else ""


def _text(value: object) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _call(event: object, name: str) -> str:
    """安全调用 ``event.get_xxx()``：取不到就空串（降级）。"""
    func = getattr(event, name, None)
    if not callable(func):
        return ""
    try:
        return _text(func())
    except Exception:  # noqa: BLE001 - 平台实现各异，一律降级
        return ""


def _extra(event: object, key: str) -> object:
    """安全读 ``event.get_extra(key)``：取不到返回 None。"""
    func = getattr(event, "get_extra", None)
    if not callable(func):
        return None
    try:
        return func(key, None)
    except Exception:  # noqa: BLE001
        return None


def _is_synthetic_sender(
    event: object, base: Session, sender_id: str, sender_name: str
) -> bool:
    """这个 sender 是不是主动轮塞进来的合成 sender。

    主判据是上游主动唤醒时挂的 ``cron_job`` extra（``core/cron/manager.py`` 的
    ``extras``）；第二条是防上游改字段名的兜底——昵称固定为 ``CRON_SENDER_NAME``
    且 id 恰好等于会话 id（真人 id 不该长这样）。误判代价很小：标注退回 id 而已。
    """
    if _extra(event, "cron_job"):
        return True
    return bool(
        sender_name == CRON_SENDER_NAME and sender_id and sender_id == base.session_id
    )


__all__ = ["CRON_SENDER_NAME", "GROUP", "PRIVATE", "UNKNOWN", "Session"]
