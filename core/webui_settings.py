"""WebUI 设置页的数据面（第 5.2 步）：把 ``_conf_schema.json`` 递归展成字段表，
再用 ``core.settings.load_settings`` 的 ``warnings`` 当**唯一的校验准绳**。

纯函数、零 astrbot 依赖（``core/`` 的硬约束）：这个模块**不碰** ``self.config``、
不落盘配置、不读宿主能力，只回答三件事——

1. **面板上有什么字段**（``describe_schema``：schema 驱动展开，前端不抄字段清单）；
2. **这次改动合不合法**（``plan_changes``：未知路径 / 只读项 / 类型不符 → 拒绝）；
3. **通过校验后长什么样**（``verify_settings``：``load_settings`` 新增 warning → 拒绝）。

落盘、备份、热生效都**不在这里**：备份的文件复制是 ``backup_config_file``（纯
路径运算，不依赖宿主），而"写配置 + 重建 ``self.settings`` + 重建巡检 job"住在
``main.py`` 的适配器方法里——``web_api/handlers.py`` 只做薄壳。

⚠️ **为什么校验不复刻一套规则**：``core/settings.py`` 才是配置的**唯一解释者**
（越界夹紧、非法回落、正则零宽拦截……）。面板另写一份规则必然漂移，所以这里的
做法是"改完交给 ``load_settings``，看它有没有**新**报 warning"——新增的
warning 必然来自被改的键（没改的键行为不会变），据此整单拒绝、一个字节不写。
这是"面板不会把配置写坏"的唯一保证。
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import settings as settings_mod

READ_ONLY: dict[str, str] = {
    "data_dir": "数据目录只能在这里看。改它＝搬走全部日记与本子，必须在 AstrBot 插件面板改完再重载插件。",
}
"""**只读项**（键 → 原因）。出现在字段表里但标 ``editable: false``，
改它返回结构化错误——数据目录的搬迁与 Layout 的建立都在插件启动期完成。"""

READ_ONLY_ENABLED_NOTE = "关掉总开关后日记、小本本、状态、主动消息全部不可用（数据保留）。"
"""总开关可改，但响应里要带上这句提示（§四#4）。"""

_EMPTY_DEFAULT: dict[str, Any] = {
    "string": "",
    "bool": False,
    "int": 0,
    "list": [],
    "dict": {},
    "object": {},
}
"""schema 里没写 ``default`` 时的兜底（按类型）——面板要能显示"恢复默认"的目标值。"""


# ---- 字段表：schema 递归展开 ---------------------------------------------------


def describe_schema(schema: Mapping[str, Any] | None) -> tuple[list[dict], list[dict]]:
    """把 schema 展成 ``(groups, fields)``。

    - ``groups``：**顶层**节点（组或叶子都算），保持 schema 的书写顺序——
      组带 ``children``，前端照它渲染分组即可，不必自己认路径层级；
    - ``fields``：**扁平**叶子表，``path`` 是点分路径（``diary.max_chars``），
      这是 ``POST settings`` 的 ``changes`` 键名。

    覆盖 schema 实际用到的五种叶子类型：``bool / string / int / list / dict``
    （``object`` 只作为分组出现）。
    """
    groups: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for key, spec in dict(schema or {}).items():
        if not isinstance(spec, Mapping):
            continue
        groups.append(_node((str(key),), spec, fields))
    return groups, fields


def _node(path: tuple[str, ...], spec: Mapping[str, Any], fields: list[dict]) -> dict[str, Any]:
    kind = str(spec.get("type") or "string")
    node: dict[str, Any] = {
        "path": ".".join(path),
        "key": path[-1],
        "type": kind,
        "description": str(spec.get("description") or path[-1]),
        "hint": str(spec.get("hint") or ""),
    }
    if kind == "object":
        node["children"] = []
        items = spec.get("items")
        for key, sub in dict(items or {}).items():
            if not isinstance(sub, Mapping):
                continue
            node["children"].append(_node((*path, str(key)), sub, fields))
        return node

    node["default"] = copy.deepcopy(spec.get("default", _EMPTY_DEFAULT.get(kind, "")))
    # 作息表这类"默认带换行"的字符串用 textarea：schema 驱动，前端不认字段名。
    node["multiline"] = isinstance(node["default"], str) and "\n" in node["default"]
    limits = settings_mod.INT_LIMITS.get(node["path"])
    if limits:
        node["min"], node["max"] = limits[0], limits[1]
    reason = READ_ONLY.get(node["path"], "")
    node["editable"] = not reason
    node["note"] = reason
    fields.append(node)
    return node


def editable_paths(fields: list[dict[str, Any]]) -> list[str]:
    """可改的点分路径（只读项不在其中）。"""
    return [str(field["path"]) for field in fields if field.get("editable")]


# ---- 值：当前生效 / 默认 -------------------------------------------------------


def values_from_settings(
    settings: settings_mod.Settings, schema: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """当前**实际生效**的值：照 ``Settings`` 快照取，**不读原始 config**。

    面板要显示的是"她现在真正按什么在跑"——原始 config 里越界的值已经被
    ``load_settings`` 夹紧过，面板显示夹紧后的值才不会骗人。
    字段取自 schema 树，所以 schema 加字段不用改这里。
    """
    groups, _ = describe_schema(schema if isinstance(schema, Mapping) else _DEFAULTS_SOURCE())
    return {node["key"]: _value_of(node, settings) for node in groups}

def defaults_from_schema(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """schema 里的默认值（"恢复默认"的目标值）。"""
    groups, _ = describe_schema(schema)
    return {node["key"]: _default_of(node) for node in groups}


def _value_of(node: Mapping[str, Any], settings: Any, container: Mapping[str, Any] | None = None) -> Any:
    """叶子取值：顶层读 ``Settings`` 的属性，组内读**上级映射的键**。

    嵌套组（``proactive.patrol_minutes``）在 ``Settings`` 上只有
    ``settings.proactive["patrol_minutes"]``，没有同名属性——所以 ``container``
    一路往下传，不重新 getattr。
    """
    if node["type"] == "object":
        source = container if container is not None else getattr(settings, str(node["key"]), None)
        source = dict(source) if isinstance(source, Mapping) else {}
        children = node.get("children") or []
        if not children:
            return _jsonable(source)
        return {str(child["key"]): _value_of(child, settings, source) for child in children}
    raw = container.get(node["key"]) if isinstance(container, Mapping) else getattr(settings, str(node["key"]), None)
    return _jsonable(raw)


def _default_of(node: Mapping[str, Any]) -> Any:
    if node["type"] == "object":
        return {child["key"]: _default_of(child) for child in node.get("children") or []}
    return _jsonable(node.get("default"))


def _jsonable(value: Any) -> Any:
    """tuple / Mapping → list / dict（json_response 之外的调用方也要能直接序列化）。"""
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(val) for key, val in value.items()}
    return value


def _DEFAULTS_SOURCE() -> dict[str, Any]:
    """按 ``Settings`` 的字段名重建一份 schema 形状（只有类型与默认值有意义）。

    仅在**拿不到**真实 schema 时兜底（``GET`` 之外的内部调用）；面板路径一律走
    ``web_api.handlers`` 里读到的 ``_conf_schema.json``。
    """
    defaults = settings_mod.default_config()
    out: dict[str, Any] = {}
    for name, value in defaults.items():
        if isinstance(value, Mapping):
            out[name] = {
                "type": "object",
                "items": {
                    key: {"type": _kind_of(item), "default": copy.deepcopy(item)}
                    for key, item in value.items()
                },
            }
        else:
            out[name] = {"type": _kind_of(value), "default": copy.deepcopy(value)}
    return out


def _kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, Mapping):
        return "dict"
    return "string"


# ---- 校验 ---------------------------------------------------------------------


def plan_changes(
    base: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
    fields: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """把 ``changes`` 合并进 ``base`` 的深拷贝：``(updated, error, applied)``。

    ``updated is None`` 即**整单拒绝**：未知点分路径、只读项、类型不符，一个都不放行。
    这里**不做业务校验**（越界/非法值的判断是 ``load_settings`` 的活，见
    ``verify_settings``）——两层职责分开，才不会各写一半规则。
    """
    if not isinstance(changes, Mapping) or not changes:
        return None, "没有需要保存的配置更改。", []

    allowed = {str(field["path"]): field for field in fields}
    updated = copy.deepcopy(dict(base)) if isinstance(base, Mapping) else {}
    applied: list[str] = []

    for raw_path, value in changes.items():
        path = str(raw_path)
        field = allowed.get(path)
        if field is None:
            return None, f"包含未知配置项「{path}」，请刷新页面后重试。", []
        if not field.get("editable", True):
            return None, f"「{field['description']}」只能看不能改：{field.get('note') or '该项为只读'}", []
        problem = _type_problem(field, value)
        if problem:
            return None, problem, []
        node, ok = _walk_to_parent(updated, path)
        if not ok:
            return None, f"配置结构异常，写不进「{path}」，未做改动。", []
        node[path.rsplit(".", 1)[-1]] = copy.deepcopy(value)
        applied.append(path)

    return updated, "", applied


def _type_problem(field: Mapping[str, Any], value: Any) -> str:
    """类型不符就是**面板的错**（不是配置的错），直接拒，不进 ``load_settings``。"""
    label = str(field.get("description") or field.get("path"))
    kind = str(field.get("type") or "string")
    if kind == "bool":
        ok = type(value) is bool
    elif kind == "int":
        ok = type(value) is int  # bool 是 int 的子类，所以用 type() 而不是 isinstance()
    elif kind == "string":
        ok = isinstance(value, str)
    elif kind == "list":
        ok = isinstance(value, list) and all(isinstance(item, str) for item in value)
    elif kind == "dict":
        ok = isinstance(value, dict) and all(
            isinstance(key, str) and isinstance(val, str) for key, val in value.items()
        )
    else:  # 面板没暴露的组类型：不该出现在 changes 里
        ok = False
    if ok:
        return ""
    return f"「{label}」的值类型不对（要 {kind}），已拒绝保存。"


def _walk_to_parent(config: dict[str, Any], path: str) -> tuple[dict[str, Any], bool]:
    node: Any = config
    for key in path.split(".")[:-1]:
        if not isinstance(node, dict):
            return config, False
        child = node.get(key)
        if child is None:
            child = {}
            node[key] = child
        if not isinstance(child, dict):
            return config, False
        node = child
    return (node if isinstance(node, dict) else config), isinstance(node, dict)


def verify_settings(
    base: Mapping[str, Any] | None, updated: Mapping[str, Any] | None
) -> tuple[settings_mod.Settings | None, list[str]]:
    """**唯一的校验准绳**：``load_settings`` 前后各跑一次，多出 warning 就整单拒绝。

    返回 ``(新 Settings, [])`` 或 ``(None, 新增的 warning 列表)``。

    为什么不逐条比对"warning 涉及哪个键"：没被改的键，其 ``load_settings`` 行为
    不会变化——**新增的 warning 必然来自被改的键**。整单判定比匹配字符串更严，
    也不会被 warning 文案变化误伤。
    """
    before = settings_mod.load_settings(base).warnings
    resolved = settings_mod.load_settings(updated)
    fresh = [item for item in resolved.warnings if item not in before]
    if fresh:
        return None, fresh
    return resolved, []


# ---- 备份 ---------------------------------------------------------------------


def backup_name(now: datetime | None = None) -> str:
    """备份文件名（秒级：一次改动的粒度就是"一次点击"）。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"config_backup_{stamp}.json"


def backup_config_file(
    config_path: str | Path | None, data_dir: str | Path, now: datetime | None = None
) -> Path | None:
    """把**旧配置文件**整份复制到数据目录。配置文件不在（首次运行）就返回 ``None``。

    失败**抛 OSError**——调用方据此整单放弃写入：备份是写盘的安全网，
    备份不下来的那次改动不该落盘。
    """
    if not config_path:
        return None
    source = Path(config_path)
    if not source.is_file():
        return None
    target_dir = Path(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / backup_name(now)
    shutil.copy2(source, target)
    return target


__all__ = [
    "READ_ONLY",
    "READ_ONLY_ENABLED_NOTE",
    "backup_config_file",
    "backup_name",
    "defaults_from_schema",
    "describe_schema",
    "editable_paths",
    "plan_changes",
    "values_from_settings",
    "verify_settings",
]
