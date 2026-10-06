"""启用范围（第 7 步）："她在哪里工作"与"谁能被主动"的**唯一判定**。

三个入口（注入钩子 / 她的写工具 / 主动消息）都只调这里的纯函数，**别在调用方
各写一遍判断**。用户拍板的三条语义（别改）：

1. **「主人」= ``love_peers``**（既有名单，不新增 ``owner_ids``）；
2. **``all`` 档允许主动消息进群**（受护栏：只找互动过的群、群里必须 @、配额沿用）；
3. **默认档 = ``private``**（只私聊）。

``love_peers`` 的语义从这一步起**收窄**：它只承担"主人身份"（``owner`` 档判定 +
恋爱日记可见性），不再是"主动消息唯一对象"——主动对象由档位决定
（``proactive_targets``），名单里的人只是 ``owner`` 档下的全部、``private`` 档下的子集。

**范围外就是整轮不介入**（不注入、不写、不观察）。⚠️ 面板与 WebUI 接口永远可用，
不受这个闸影响——否则用户切到 ``owner`` 之后，在群里打开面板会被自己锁死。

零 astrbot import（``core/`` 硬约束）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MODES = ("owner", "private", "all")
DEFAULT_MODE = "private"

_MODE_LABELS: dict[str, str] = {"owner": "只主人", "private": "只私聊", "all": "全部启用"}


def mode_label(mode: str) -> str:
    """档位的中文名（日志与拒绝话术用；未知值按默认档称呼）。"""
    return _MODE_LABELS.get(mode if mode in MODES else DEFAULT_MODE, _MODE_LABELS[DEFAULT_MODE])


def normalize_mode(raw: object) -> tuple[str, str]:
    """未知值回落默认并给一句人话原因（照 ``core/settings.py`` 的既有纪律）。

    没配（``None``）不算错——返回默认、不带 warning；给了但不认识（含非字符串、
    空串）才回落 + warning。大小写与首尾空白宽容（``" ALL "`` 合法）。
    """
    if raw is None:
        return DEFAULT_MODE, ""
    text = raw.strip().lower() if isinstance(raw, str) else ""
    if text in MODES:
        return text, ""
    return DEFAULT_MODE, (
        f"scope.mode 取值不认识（{raw!r}），回落「{_MODE_LABELS[DEFAULT_MODE]}」"
        f"（可选：{' / '.join(f'{mode}={_MODE_LABELS[mode]}' for mode in MODES)}）"
    )


def allows(
    mode: str,
    *,
    is_private: bool,
    person_id: str,
    love_peers: Sequence[str],
) -> tuple[bool, str]:
    """这一轮该不该介入 → ``(能不能工作, 人话原因)``（原因只在拒绝时非空）。

    - ``owner``：只 ``love_peers`` 里那个人的**私聊**；
    - ``private``：任何私聊；
    - ``all``：私聊 + 群聊一律放行。
    """
    if mode not in MODES:
        mode = DEFAULT_MODE  # 防御：调用方忘了先 normalize_mode
    if is_private:
        if mode == "owner" and str(person_id or "") not in {str(peer) for peer in love_peers}:
            return False, "现在是「只主人」档，这里只服务最亲密的人"
        return True, ""
    if mode == "all":
        return True, ""
    if mode == "owner":
        return False, "现在是「只主人」档，群聊不启用"
    return False, "现在是「只私聊」档，群聊不启用（要群聊请切「全部启用」）"


def proactive_targets(
    mode: str,
    *,
    contacts: Mapping[str, Any],
    love_peers: Sequence[str],
    sessions: Mapping[str, Any] | None = None,
    known_groups: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """主动消息的目标集合 → ``[(umo, kind)]``，``kind`` ∈ ``{"private", "group"}``。

    - ``owner``：只主人的私聊；
    - ``private``：所有**互动过**的私聊（∪ 名单里的人）；
    - ``all``：上面的 + **互动过的群**。

    **目标来源只有一个：``contacts``**（proactive.json 的联系人表，``note_contact``
    在每个被动轮记录"谁在哪跟我说话"）——从没互动过的人、没见过的群，在任何档位下
    都进不来（这条是硬的）。``known_groups`` 是调用方从 ``contacts`` 归并出的
    ``kind=="group"`` 群 umo 清单（她真在里面说过话的群）；``sessions`` 只参与
    **稳定排序**（已有主动轨迹的会话排前，延续既有关系优先于新联系人），
    不扩大目标集合。
    """
    if mode not in MODES:
        mode = DEFAULT_MODE
    contact_items = [
        (str(person), contact)
        for person, contact in dict(contacts or {}).items()
        if isinstance(contact, dict)
    ]
    peers = [str(peer) for peer in (love_peers or [])]
    peer_set = set(peers)

    privates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def take(person: str, contact: Mapping[str, Any]) -> None:
        if str(contact.get("kind") or "") != "private":
            return
        umo = str(contact.get("umo") or "")
        if not umo or umo in seen:
            return
        seen.add(umo)
        privates.append((umo, "private"))

    for peer in peers:  # 名单里的人排最前（名单序）——owner 档的遍历顺序与既有行为一致
        for person, contact in contact_items:
            if person == peer:
                take(person, contact)
    for person, contact in contact_items:
        if mode == "owner" and person not in peer_set:
            continue
        take(person, contact)

    if mode != "all":
        return _order(privates, sessions)

    groups: list[tuple[str, str]] = []
    group_seen: set[str] = set()
    for umo in known_groups or ():
        umo = str(umo or "")
        if umo and umo not in group_seen and umo not in seen:
            group_seen.add(umo)
            groups.append((umo, "group"))
    return _order(privates + groups, sessions)


def _order(
    targets: list[tuple[str, str]], sessions: Mapping[str, Any] | None
) -> list[tuple[str, str]]:
    """稳定排序：已有主动轨迹（sessions 表里有条目）的会话排前，其余保持原序。"""
    known = sessions if isinstance(sessions, Mapping) else {}
    keyed = [(umo, kind, 0 if umo in known else 1) for umo, kind in targets]
    keyed.sort(key=lambda item: item[2])
    return [(umo, kind) for umo, kind, _ in keyed]


__all__ = ["DEFAULT_MODE", "MODES", "allows", "mode_label", "normalize_mode", "proactive_targets"]
