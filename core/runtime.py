"""按 Bot 实例（scope）隔离的运行时——日记多 Bot 隔离的**纯内核基础**（P0）。

**为什么需要这一层**
-------------------
一个 AstrBot 进程里可以同时挂多个平台实例（个人 QQ 的 aiocqhttp、QQ 官方 bot……）。
它们共用一个进程，但**不应该共用日记的数据**：日记、小本本、情绪、熟悉度、生活日程、
主动决策、群社交、立绘、生图缓存都必须**按实例分开**。

本模块是**内核**（``core/`` 的硬约束：**零 astrbot import**），只负责两件事：

1. **把一组业务门面按 scope 组装起来**（``DiaryRuntime``）；
2. **把 scope 映射到各自独立的数据根**（``DiaryRuntimeManager``）。

**归属怎么定**
--------------
只用**真实平台实例 ID**：``event.get_platform_id()``（或等价的 ``platform_id`` 属性）。
适配器类型名（``aiocqhttp`` / ``qq_official``）与**自报昵称**都不作数——
同一类型可能挂多个实例，拿类型当隔离键＝两个 Bot 共用一个日记本。

拿不到实例 ID 时**拒绝读写**（``resolve`` 返回 ``None``），绝不悄悄回落共享默认桶。

**目录布局**
-----------
    <base_data_dir>/                     legacy 桶（显式绑定的老实例用它）
    <base_data_dir>/bots/<sha256(scope)>/ 每个其它 scope 的独立数据根

新桶**只建空目录，不复制任何用户数据**（本插件没有需要复制的模板文件，
所以"只复制模板"= 什么都不复制）。每个桶写一份 ``instance.json`` 记归属：
**legacy 只在显式绑定时记归属**，改名（重绑）不会悄悄认领旧数据；
元信息损坏或与当前 scope 不一致时**fail closed**（拒绝服务，而不是覆盖它）。

**本轮边界**
------------
这里只提供内核与稳定 API；``main.py`` 的接线（事件 → scope → runtime）与 WebUI 的
实例选择器属于后续任务。**不**改 ``Session.person_id`` 的语义，
**不**改任何底层存储文件的格式。
"""

from __future__ import annotations

import hashlib
import json
import copy
from datetime import datetime, timezone
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import settings as settings_mod
from . import storage
from ._log import logger
from .diary.api import Diary
from .diary.comments import CommentStore
from .diary.store import DiaryStore, person_id_by_name
from .greeting.api import Greeting
from .greeting.store import GreetingStore
from .imagery import Imagery
from .life import Life
from .notebook import Notebook, NotebookStore
from .proactive import Proactive
from .proactive.store import ProactiveStore
from .social import Social
from .state import Affinity, State
from .state.store import AffinityStore, StateStore
from .webui_portrait import PortraitStore

LEGACY_SCOPE = "__legacy__"
"""内部键：legacy 桶（原始数据根）在运行时的登记名。**不是**对外的 scope ID。"""

INSTANCE_FILE = "instance.json"
"""每个数据根的归属元信息文件。"""

INSTANCE_SCHEMA_VERSION = 1
"""``instance.json`` 的结构版本。不认识 → fail closed。"""

BOTS_DIRNAME = "bots"
"""非 legacy scope 的数据根父目录名。"""

_SCOPE_MAX_LEN = 200
"""scope ID 的长度上限：超长的一律当作坏 scope 拒绝（防目录名被撑爆）。"""


# ---- scope 解析 ----------------------------------------------------------------


def scope_id_from_event(event: object) -> str:
    """事件的**真实平台实例 ID**。

    只认 ``get_platform_id()``（或等价的 ``platform_id`` 属性）。
    **不**用适配器类型名，也**不用** ``self_id`` / 猜 QQ 号与 OpenID / 自报昵称。
    取不到返回空串（调用方必须拒绝本轮读写）。
    """
    pid = ""
    getter = getattr(event, "get_platform_id", None)
    if callable(getter):
        try:
            pid = getter()
        except Exception as error:  # noqa: BLE001 - 平台实现各异，一律降级
            logger.warning("[runtime] 取 get_platform_id 失败：%s", error)
            pid = ""
    if not pid:
        pid = getattr(event, "platform_id", "") or ""
    return str(pid).strip()


def scope_id_from_umo(umo: str) -> str:
    """UMO 首段的**显式**解析辅助。

    UMO 形如 ``<平台实例ID>:<消息类型>:<会话ID>``。⚠️ 只有当首段**确实是平台实例 ID**
    时用它；老部署里首段常常是适配器类型名（``aiocqhttp``），那**不是**隔离键。
    本函数不做"是不是类型名"的猜测——判断归调用方。
    """
    text = str(umo or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def scope_id_from_session(session: object) -> str:
    """从 ``Session`` 取候选 scope（``session.platform``，即 UMO 首段）。

    与 :func:`scope_id_from_umo` 同源，语义相同：调用方必须确认它填的是实例 ID。
    """
    platform = getattr(session, "platform", "") or ""
    return str(platform).strip()


def is_valid_scope(scope_id: str) -> bool:
    """scope ID 是否可用（空串、过长、控制字符一律拒绝）。"""
    text = str(scope_id or "").strip()
    if not text or len(text) > _SCOPE_MAX_LEN:
        return False
    return not any(ord(ch) < 32 for ch in text)


def scope_dir_name(scope_id: str) -> str:
    """scope → 目录名。sha256 编码：平台 ID 里的路径分隔符等字符全部失效。"""
    return hashlib.sha256(str(scope_id or "").encode("utf-8")).hexdigest()


# ---- 归属元信息 ---------------------------------------------------------------


@dataclass(frozen=True)
class InstanceMeta:
    """一个数据根的归属记录（``instance.json`` 的内容）。"""

    scope_id: str
    kind: str
    created_at: str = ""
    schema_version: int = INSTANCE_SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "scope_id": self.scope_id,
            "created_at": self.created_at,
        }


def read_instance_meta(base_dir: Path | str) -> InstanceMeta | None:
    """读 ``instance.json``。

    - 文件不存在 → ``None``（"还没记归属"，不等于"坏了"）；
    - JSON 坏了 / 顶层不是对象 / 版本不认识 / 缺字段 → **fail closed**：抛
      ``ValueError``（调用方按"拒绝服务"处理，**不**覆盖、不猜测）。
    """
    path = Path(base_dir) / INSTANCE_FILE
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise ValueError("归属元信息不能是链接")
    try:
        if path.stat().st_size > 16384:
            raise ValueError("归属元信息过大")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"读归属元信息失败（{path}）：{error}") from error
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError as error:
        raise ValueError(f"归属元信息不是合法 JSON（{path}）：{error}") from error
    if not isinstance(doc, dict):
        raise ValueError(f"归属元信息顶层不是对象（{path}）")
    version = doc.get("schema_version")
    if type(version) is not int or version != INSTANCE_SCHEMA_VERSION:
        raise ValueError(f"归属元信息版本不认识（{path}）：{version!r}")
    scope_id = doc.get("scope_id")
    kind = doc.get("kind")
    if not isinstance(scope_id, str) or not is_valid_scope(scope_id) or kind not in ("bot", "legacy"):
        raise ValueError(f"归属元信息缺字段（{path}）")
    return InstanceMeta(
        scope_id=scope_id,
        kind=kind,
        created_at=str(doc.get("created_at") or ""),
        schema_version=INSTANCE_SCHEMA_VERSION,
    )


def write_instance_meta(base_dir: Path | str, meta: InstanceMeta) -> None:
    """写 ``instance.json``（原子写）。"""
    path = Path(base_dir) / INSTANCE_FILE
    storage.atomic_write_text(
        path, json.dumps(meta.to_json(), ensure_ascii=False, indent=2) + storage.LINE_SEP
    )


# ---- 运行时 --------------------------------------------------------------------


@dataclass
class DiaryRuntime:
    """一个 scope 的**全部**可写状态：业务门面 + 它的目录 + 它的锁。

    每个实例一份 ``settings``（含该实例的 per-instance 覆盖）、一份 ``layout``
    （数据根）、一份 ``locks``（同实例内按文件串行）。
    """

    scope: str
    settings: settings_mod.Settings
    layout: storage.Layout
    locks: storage.KeyedLocks
    diary: Diary
    notebook: Notebook
    state: State
    life: Life
    social: Social
    affinity: Affinity
    proactive: Proactive
    greeting: Greeting
    portraits: PortraitStore
    imagery: Imagery
    comments: CommentStore
    legacy: bool = False

    @property
    def base_dir(self) -> Path:
        return self.layout.base_dir

    def describe(self) -> str:
        return f"{self.scope} → {self.layout.base_dir}"


def build_runtime(
    scope: str,
    settings: settings_mod.Settings,
    base_dir: Path | str,
    *,
    legacy: bool = False,
) -> DiaryRuntime:
    """组装一个 scope 的全部业务门面。

    构造顺序与 ``main.py`` 的插件构造函数一致，但**搬到这里**：
    插件本体只留一份 legacy 门面（后续接线任务再改），实例的差异只由这里产生。

    ⚠️ 旧纯文本日记导入时按"标注名 → person_id"回填，闭包必须用**本 runtime 的**
    ``settings.name_preference`` 与**本 runtime** 的联系人表——否则两个实例会互相
    认领对方的联系人。
    """
    layout = storage.Layout(Path(base_dir)).ensure()
    locks = storage.KeyedLocks()
    runtime = DiaryRuntime(
        scope=scope,
        settings=settings,
        layout=layout,
        locks=locks,
        diary=None,  # type: ignore[arg-type]  # 下面就地替换（循环依赖：先建门面再补引用）
        notebook=None,  # type: ignore[arg-type]
        state=None,  # type: ignore[arg-type]
        life=None,  # type: ignore[arg-type]
        social=None,  # type: ignore[arg-type]
        affinity=None,  # type: ignore[arg-type]
        proactive=None,  # type: ignore[arg-type]
        greeting=None,  # type: ignore[arg-type]
        portraits=None,  # type: ignore[arg-type]
        imagery=None,  # type: ignore[arg-type]
        comments=None,  # type: ignore[arg-type]
        legacy=legacy,
    )

    def _name_lookup(who: str) -> str:
        """旧头行标注名 → person_id（只在本实例的联系人表与称呼偏好里找）。"""
        try:
            contacts = runtime.proactive.store.contacts()
        except Exception:  # noqa: BLE001 - 联系人表坏了不能带累导入
            contacts = {}
        return person_id_by_name(who, dict(runtime.settings.name_preference or {}), contacts)

    runtime.diary = Diary(
        settings=settings,
        layout=layout,
        store=DiaryStore(layout=layout, locks=locks, name_lookup=_name_lookup),
        locks=locks,
    )
    runtime.notebook = Notebook(
        settings=settings,
        layout=layout,
        store=NotebookStore(layout=layout, locks=locks),
        locks=locks,
    )
    runtime.state = State(
        settings=settings,
        layout=layout,
        store=StateStore(layout=layout, locks=locks),
        locks=locks,
    )
    runtime.life = Life(settings=settings, layout=layout, locks=locks)
    runtime.social = Social(settings=settings, layout=layout, locks=locks, life=runtime.life)
    runtime.affinity = Affinity(
        settings=settings,
        layout=layout,
        store=AffinityStore(layout=layout, locks=locks),
        locks=locks,
    )
    runtime.proactive = Proactive(
        settings=settings,
        layout=layout,
        store=ProactiveStore(layout=layout, locks=locks),
        diary=runtime.diary,
        notebook=runtime.notebook,
        state=runtime.state,
        affinity=runtime.affinity,
        life=runtime.life,
        social=runtime.social,
    )
    runtime.greeting = Greeting(
        settings=settings,
        layout=layout,
        store=GreetingStore(layout=layout, locks=locks),
        locks=locks,
    )
    runtime.portraits = PortraitStore(layout=layout, locks=locks)
    runtime.imagery = Imagery(
        settings=settings,
        layout=layout,
        locks=locks,
        portraits=runtime.portraits,
        life=runtime.life,
        state=runtime.state,
    )
    runtime.comments = CommentStore(layout, locks, runtime.diary.store)
    return runtime


# ---- 管理器 --------------------------------------------------------------------


class DiaryRuntimeManager:
    """scope → ``DiaryRuntime`` 的登记处。隔离开关、数据根归属也在这里。

    - **数据根固定**：``base_data_dir`` 在构造时给定，之后**永不改变**
      （配置热更新只换 settings，不搬数据）；
    - **隔离关闭**（默认）→ 保持老行为：所有 scope 共用 legacy 桶；
    - **隔离开启** → 缺 scope 一律拒绝（``None``），**不**默认回落 legacy；
      只有**显式绑定**的 ``legacy_platform_id`` 才回到原数据根。
    """

    def __init__(
        self,
        base_data_dir: Path | str,
        raw_config: Mapping[str, Any] | None = None,
    ) -> None:
        self.base_data_dir = Path(base_data_dir).resolve()
        self._raw_config: dict[str, Any] = copy.deepcopy(dict(raw_config or {}))
        self._settings = settings_mod.load_settings(self._raw_config)
        self._runtimes: dict[str, DiaryRuntime] = {}
        self.errors: list[str] = []
        """最近一次 fail closed 的原因（给面板/日志看，不抛异常打断宿主）。"""

    # ---- 隔离开关 ----

    @property
    def settings(self) -> settings_mod.Settings:
        """全局配置快照。"""
        return self._settings

    @property
    def raw_config(self) -> dict[str, Any]:
        return copy.deepcopy(self._raw_config)

    @property
    def isolation_on(self) -> bool:
        """多 Bot 隔离是否开启（默认关＝保持老部署行为）。"""
        return bool(self._settings.bot_isolation.get("enable", False))

    @property
    def legacy_platform_id(self) -> str:
        """显式绑定回原数据根的平台实例 ID（空＝没人绑，谁都读不到旧数据）。"""
        return str(self._settings.bot_isolation.get("legacy_platform_id") or "").strip()

    # ---- legacy 桶 ----

    @property
    def legacy_runtime(self) -> DiaryRuntime:
        """legacy 桶（原始数据根）的运行时。

        允许保留它供兼容，但**隔离开启时不能经无 scope 的访问拿到它**
        （见 :meth:`resolve`）。
        """
        if self.isolation_on and not self.legacy_platform_id:
            raise ValueError("未显式绑定旧实例，禁止读取旧数据")
        if self.isolation_on:
            self._claim_legacy()
        runtime = self._runtimes.get(LEGACY_SCOPE)
        if runtime is None:
            runtime = self._build_legacy_runtime()
            self._runtimes[LEGACY_SCOPE] = runtime
        return runtime

    def _build_legacy_runtime(self) -> DiaryRuntime:
        scoped = self._scoped_settings(self._legacy_scope_key())
        return build_runtime(
            self._legacy_scope_key(), scoped, self.base_data_dir, legacy=True
        )

    def _claim_legacy(self) -> None:
        """Persist the explicit owner before exposing any legacy business data."""
        existing = read_instance_meta(self.base_data_dir)
        if existing is not None:
            if existing.kind != "legacy" or existing.scope_id != self.legacy_platform_id:
                raise ValueError("旧数据已属于其他实例，禁止重新认领")
            return
        if LEGACY_SCOPE in self._runtimes:
            raise ValueError("旧实例归属记录丢失，停止访问并保留数据")
        self.base_data_dir.mkdir(parents=True, exist_ok=True)
        write_instance_meta(self.base_data_dir, InstanceMeta(
            scope_id=self.legacy_platform_id, kind="legacy",
            created_at=datetime.now(timezone.utc).isoformat(),
        ))

    def _remember_error(self, error: object) -> None:
        self.errors.append(str(error))
        del self.errors[:-32]

    def _safe_scope_path(self, scope: str) -> Path:
        parent = self.base_data_dir / BOTS_DIRNAME
        target = parent / scope_dir_name(scope)
        for candidate in (parent, target):
            if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                raise ValueError("实例数据目录不能是链接或 junction")
        if not target.resolve().is_relative_to(self.base_data_dir):
            raise ValueError("实例数据目录越出插件数据根")
        if target.exists() and not target.is_dir():
            raise ValueError("实例数据路径不是目录")
        return target

    def _legacy_scope_key(self) -> str:
        """legacy 桶的 per-instance 覆盖键。

        隔离关闭时用内部键（全局行为不变）；隔离开启且显式绑定了实例时，
        用那个实例 ID——这样"显式绑定的老实例"也能有它自己的覆盖。
        """
        if not self.isolation_on:
            return LEGACY_SCOPE
        return self.legacy_platform_id or LEGACY_SCOPE

    # ---- 解析 ----

    def resolve(self, scope_id: str) -> DiaryRuntime | None:
        """按 scope 取运行时。

        - 隔离**关闭** → 一切回到 legacy 桶（老行为，不破坏现有部署）；
        - 隔离**开启** → 空 scope / 坏 scope 一律 ``None``（拒绝，不默认 legacy）；
          显式绑定的 ``legacy_platform_id`` → legacy 桶；其它 scope → 独立数据根。

        归属元信息损坏或与当前 scope 不一致 → ``None``（fail closed），
        原因记进 :attr:`errors`。
        """
        scope = str(scope_id or "").strip()
        if not self.isolation_on:
            return self.legacy_runtime
        if not is_valid_scope(scope):
            self._remember_error("scope 无效，拒绝访问")
            return None
        if self.legacy_platform_id and scope == self.legacy_platform_id:
            try:
                return self.legacy_runtime
            except (ValueError, OSError) as error:
                self._remember_error(error)
                return None
        runtime = self._runtimes.get(scope)
        if runtime is not None:
            try:
                target = self._safe_scope_path(scope)
                existing = read_instance_meta(target)
                if existing is None or existing.scope_id != scope or existing.kind != "bot":
                    raise ValueError("实例归属记录已改变或丢失，停止访问")
            except (ValueError, OSError) as error:
                self._remember_error(error)
                return None
        if runtime is None:
            runtime = self._create_scoped_runtime(scope)
            if runtime is None:
                return None
            self._runtimes[scope] = runtime
        return runtime

    def resolve_event(self, event: object) -> DiaryRuntime | None:
        """事件 → 运行时。拿不到唯一实例 ID 时返回 ``None``（调用方须拒绝本轮）。"""
        if not self.isolation_on:
            return self.legacy_runtime
        scope = scope_id_from_event(event)
        if not is_valid_scope(scope):
            self._remember_error("事件没有唯一平台实例 ID，本轮拒绝读写")
            logger.warning("[runtime] 事件没有唯一平台实例 ID，本轮拒绝读写")
            return None
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if umo and scope_id_from_umo(umo) != scope:
            self._remember_error("事件实例与统一消息源不一致，拒绝读写")
            return None
        return self.resolve(scope)

    def _scoped_dir(self, scope: str) -> Path:
        return self.base_data_dir / BOTS_DIRNAME / scope_dir_name(scope)

    def _create_scoped_runtime(self, scope: str) -> DiaryRuntime | None:
        """为非 legacy scope 建独立数据根。

        - **不复制任何用户数据**（本插件没有要复制的模板 → 什么都不复制）；
        - 已有目录但归属元信息对不上 / 坏了 → fail closed（拒绝服务，不覆盖）；
        - 新目录 → 写 ``instance.json`` 记归属。
        """
        try:
            target = self._safe_scope_path(scope)
            existing = read_instance_meta(target)
        except (ValueError, OSError) as error:
            self._remember_error(error)
            logger.warning("[runtime] %s", error)
            return None
        if existing is not None:
            if existing.scope_id != scope or existing.kind != "bot":
                message = (
                    f"数据根归属与当前 scope 不一致（{target}：记的是 {existing.scope_id!r}，"
                    f"当前是 {scope!r}），拒绝访问"
                )
                self._remember_error(message)
                logger.warning("[runtime] %s", message)
                return None
        else:
            if target.exists() and any(target.iterdir()):
                message = f"数据根 {target} 已有内容但没有归属记录，归属不明，拒绝访问"
                self._remember_error(message)
                logger.warning("[runtime] %s", message)
                return None
            try:
                storage.ensure_dir(target)
                write_instance_meta(
                    target,
                    InstanceMeta(
                        scope_id=scope,
                        kind="bot",
                        created_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except OSError as error:
                self._remember_error(error)
                logger.warning("[runtime] 建数据根失败（%s）：%s", target, error)
                return None
        scoped = self._scoped_settings(scope)
        runtime = build_runtime(scope, scoped, target, legacy=False)
        logger.info("[runtime] 实例数据根：%s", runtime.describe())
        return runtime

    def _scoped_settings(self, scope: str) -> settings_mod.Settings:
        """该 scope 生效的配置 = 全局配置 + per-instance 白名单覆盖。"""
        return settings_mod.build_scope_settings(self._raw_config, scope)

    # ---- 枚举 ----

    def all_runtimes(self) -> list[DiaryRuntime]:
        """已知运行时列表（legacy 桶 + 已建实例）。"""
        return list(self._runtimes.values())

    def list_instances(self) -> list[dict[str, Any]]:
        """已知实例的元信息（面板/日志用）。

        含：legacy 桶（隔离关闭或显式绑定时）、磁盘上已存在的实例桶
        （读它们的 ``instance.json``）、以及本次进程已建好的运行时。
        """
        seen: dict[str, dict[str, Any]] = {}

        def _put(scope: str, kind: str, base_dir: Path, created_at: str = "") -> None:
            if scope in seen:
                return
            seen[scope] = {
                "scope_id": scope,
                "kind": kind,
                "legacy": kind == "legacy",
                "data_dir": str(base_dir),
                "created_at": created_at,
            }

        if not self.isolation_on or self.legacy_platform_id:
            _put(
                self._legacy_scope_key(),
                "legacy",
                self.base_data_dir,
            )
        bots_root = self.base_data_dir / BOTS_DIRNAME
        if bots_root.is_symlink() or (hasattr(bots_root, "is_junction") and bots_root.is_junction()):
            return list(seen.values())
        if bots_root.is_dir():
            for child in sorted(bots_root.iterdir()):
                if not child.is_dir():
                    continue
                try:
                    meta = read_instance_meta(child)
                except ValueError:
                    continue  # 归属不明/损坏的目录不进实例列表（fail closed）
                if meta is None or meta.kind != "bot" or child.name != scope_dir_name(meta.scope_id):
                    continue
                try:
                    self._safe_scope_path(meta.scope_id)
                except (ValueError, OSError):
                    continue
                _put(meta.scope_id, "bot", child, meta.created_at)
        for scope, runtime in self._runtimes.items():
            _put(
                runtime.scope,
                "legacy" if runtime.legacy else "bot",
                runtime.base_dir,
            )
        return list(seen.values())

    # ---- 配置热更新 ----

    def update_config(self, raw_config: Mapping[str, Any] | None) -> None:
        """配置热更新：**只换 settings**，不搬数据、不换数据根、不废掉旧引用。

        - 旧引用（已拿在手里的 ``DiaryRuntime`` 与各业务门面）继续可用，
          它们的 ``settings`` 指向新快照（与 ``main.py`` 的 ``apply_settings`` 同款做法）；
        - ``base_data_dir`` **不变**；
        - 新实例按新配置建；已存在的实例按新配置就地换 settings。

        ⚠️ 本方法**同步**完成，不跨 await 改全局对象（避免与并发回合互相覆盖）。
        """
        candidate = copy.deepcopy(dict(raw_config or {}))
        resolved = settings_mod.load_settings(candidate)
        next_route = (bool(resolved.bot_isolation.get("enable")), str(resolved.bot_isolation.get("legacy_platform_id") or ""))
        current_route = (self.isolation_on, self.legacy_platform_id)
        if self._runtimes and next_route != current_route:
            raise ValueError("运行中不能改变隔离开关或旧实例归属，请安全重载插件")
        self._raw_config = candidate
        self._settings = resolved
        for scope, runtime in self._runtimes.items():
            self._apply_settings(runtime, self._scoped_settings(self._scope_key_of(runtime, scope)))

    def _scope_key_of(self, runtime: DiaryRuntime, stored_key: str) -> str:
        """运行时的 per-instance 覆盖键。"""
        if runtime.legacy:
            return self._legacy_scope_key()
        return stored_key

    @staticmethod
    def _apply_settings(runtime: DiaryRuntime, resolved: settings_mod.Settings) -> None:
        """把新 settings 就地换到 runtime 与它的门面上（不重建门面）。"""
        runtime.settings = resolved
        for facade in (
            runtime.diary,
            runtime.notebook,
            runtime.state,
            runtime.affinity,
            runtime.proactive,
            runtime.social,
            runtime.greeting,
            runtime.life,
            runtime.imagery,
        ):
            if hasattr(facade, "settings"):
                facade.settings = resolved

    def describe(self) -> str:
        parts = [f"legacy={self.base_data_dir}"]
        for runtime in self.all_runtimes():
            if runtime.legacy:
                continue
            parts.append(f"{runtime.scope}={runtime.base_dir}")
        return " | ".join(parts)


__all__ = [
    "BOTS_DIRNAME",
    "INSTANCE_FILE",
    "INSTANCE_SCHEMA_VERSION",
    "LEGACY_SCOPE",
    "DiaryRuntime",
    "DiaryRuntimeManager",
    "InstanceMeta",
    "build_runtime",
    "is_valid_scope",
    "read_instance_meta",
    "scope_dir_name",
    "scope_id_from_event",
    "scope_id_from_session",
    "scope_id_from_umo",
    "write_instance_meta",
]
