"""语音产物的**台账与清理**（F3）：谁生成的、引用到什么时候、什么时候删。

上游事实（宿主 4.28.1，实证见交付报告）：宿主 TTS 源（如 edge_tts）把成品音频写在
宿主 ``data/temp/``，**成功产物从不删**（只清中间文件）——不记账就会无限积累。

规则（面板可查、日志可对）：

- **引用期**：合成出的音频只服务"这一条主动消息的发送"；发送本身在
  ``send_message_to_user`` 工具执行内完成，宿主文件服务（若启用）的令牌 300 秒过期。
  所以最短保留只需盖住"合成 → 发送 → 文件服务令牌"这一窗，取 10 分钟保底，
  任何清理都**不碰**比这新的文件——发送前被删等于白合成。
- **保留期**：超过 ``retention_hours``（默认 6 小时）的条目可删；台账超出
  ``max_entries``（默认 200 条）时先删最老的（同样不碰保底期内的）。
- **只删自己记的**：台账按绝对路径记账，清理只对台账里的路径动手——宿主 temp 里
  别的东西（转写缓存、别的插件的文件）一个不碰。

纯逻辑与 IO 分离：``plan_cleanup`` 是纯函数；``load`` / ``save`` 用 ``core.storage``
的原子写（路径由适配层给，本模块不假设目录布局）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .. import storage

LEDGER_FILENAME = "voice-audio-ledger.json"
"""台账文件名：放在插件数据目录（``layout.base_dir``）下，不占 ``Layout`` 的固定文件表。"""

RETENTION_HOURS_DEFAULT = 6
MAX_ENTRIES_DEFAULT = 200
MIN_KEEP_SECONDS_DEFAULT = 600.0
"""引用保底期（秒）：比这新的音频绝不删（合成 → 发送 → 文件服务令牌的最坏窗口）。"""

RETENTION_HOURS_LIMIT = (1, 24 * 14)
MAX_ENTRIES_LIMIT = (20, 2000)
"""配置夹紧边界。"""

MAX_DELETES_PER_SWEEP = 100
"""一次巡检最多删多少个文件：清历史欠账也别一次 IO 几百个文件卡住巡检。"""


def clamp_retention_hours(value: Any, default: float = RETENTION_HOURS_DEFAULT) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return max(RETENTION_HOURS_LIMIT[0], min(RETENTION_HOURS_LIMIT[1], num))


def clamp_max_entries(value: Any, default: int = MAX_ENTRIES_DEFAULT) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(MAX_ENTRIES_LIMIT[0], min(MAX_ENTRIES_LIMIT[1], num))


def normalize(entries: Any) -> dict[str, str]:
    """把台账文件内容规整成 ``{绝对路径: ISO 时间}``：坏形丢弃，路径去空。

    ``storage.atomic_write_json`` 会包一层 ``schema_version`` 信封键——那是版本
    机制的字段，不是台账条目，这里剔掉。
    """
    if not isinstance(entries, dict):
        return {}
    out: dict[str, str] = {}
    for path, ts in entries.items():
        path_text = str(path or "").strip()
        ts_text = str(ts or "").strip()
        if path_text == "schema_version":
            continue
        if path_text and ts_text:
            out[path_text] = ts_text
    return out


def record(entries: dict[str, str], path: str, now: datetime) -> dict[str, str]:
    """记一笔（纯函数）：同一路径刷新时间戳；返回新 dict 不改原对象。"""
    path_text = str(path or "").strip()
    if not path_text:
        return dict(entries)
    out = dict(entries)
    out[path_text] = now.isoformat(timespec="seconds")
    return out


def _parse_ts(ts_text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts_text)
    except (TypeError, ValueError):
        return None


def safe_owned_path(path: Any, root: Any) -> Path | None:
    """返回专属产物目录的直接子文件规范路径；其他路径都拒绝。

    ``resolve`` 会跟随已有符号链接，因此指向缓存目录外的链接也不会被当成自有
    产物。根目录必须已存在且自身不能是符号链接或 junction；调用方仍应只对台账
    中由插件复制生成的文件执行删除。
    """
    try:
        root_input = Path(root).absolute()
        candidate_input = Path(path).absolute()
        is_junction = getattr(Path, "is_junction", None)
        if (
            root_input.is_symlink()
            or (is_junction is not None and root_input.is_junction())
            or not root_input.is_dir()
            or candidate_input.is_symlink()
            or (is_junction is not None and candidate_input.is_junction())
        ):
            return None
        root_path = root_input.resolve(strict=True)
        candidate = candidate_input.resolve(strict=False)
        if candidate.parent != root_path:
            return None
        if candidate.exists() and not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def plan_cleanup(
    entries: dict[str, str],
    now: datetime,
    *,
    retention_hours: float = RETENTION_HOURS_DEFAULT,
    max_entries: int = MAX_ENTRIES_DEFAULT,
    min_keep_seconds: float = MIN_KEEP_SECONDS_DEFAULT,
) -> list[str]:
    """算出这一轮**该删哪些文件**（纯函数，只出名单不动文件）。

    - 超过保留期（``retention_hours``）的先入围，最老的排前面；
    - 台账超容（``max_entries``）时，保底期外的老条目也入围补刀；
    - 保底期（``min_keep_seconds``）内的一个不碰——发送前被删等于白合成；
    - 单轮上限 ``MAX_DELETES_PER_SWEEP``，剩下的下轮再说。
    """
    safe_entries = normalize(entries)
    parsed = [(path, _parse_ts(ts_text)) for path, ts_text in safe_entries.items()]
    # Legacy naive timestamps are interpreted in the reference timezone. If ``now`` is
    # naive but the ledger already contains aware timestamps, use the first such zone;
    # otherwise use UTC. This keeps comparisons defined without changing elapsed ages.
    reference_tz = now.tzinfo if now.utcoffset() is not None else None
    if reference_tz is None:
        reference_tz = next(
            (moment.tzinfo for _, moment in parsed
             if moment is not None and moment.utcoffset() is not None),
            None,
        )
    if reference_tz is None:
        from datetime import timezone

        reference_tz = timezone.utc

    def comparable(moment: datetime) -> datetime:
        if moment.utcoffset() is None:
            return moment.replace(tzinfo=reference_tz)
        return moment.astimezone(reference_tz)

    now_comparable = comparable(now)
    min_keep_cutoff = now_comparable - timedelta(seconds=max(0.0, float(min_keep_seconds)))
    retention_cutoff = now_comparable - timedelta(hours=max(0.0, float(retention_hours)))

    aged: list[tuple[str, datetime]] = []
    for path, raw_moment in parsed:
        moment = comparable(raw_moment) if raw_moment is not None else None
        if moment is None:
            continue  # 时间戳坏了不敢删（宁多留不错删）
        if moment > min_keep_cutoff:
            continue  # 引用保底期内：绝不删
        if moment <= retention_cutoff:
            aged.append((path, moment))

    over = max(0, len(safe_entries) - max(0, int(max_entries)) - len(aged))
    if over > 0:
        aged_paths = {path for path, _ in aged}
        rest = sorted(
            (
                (path, moment)
                for path, raw_moment in parsed
                for moment in ([comparable(raw_moment)] if raw_moment is not None else [])
                if moment <= min_keep_cutoff and path not in aged_paths
            ),
            key=lambda item: item[1],
        )
        aged.extend(rest[:over])

    aged.sort(key=lambda item: item[1])
    return [path for path, _ in aged[:MAX_DELETES_PER_SWEEP]]


def prune(entries: dict[str, str], paths: list[str]) -> dict[str, str]:
    """把已删除（或删除失败但决定放弃）的路径从台账里划掉（纯函数）。"""
    drop = {str(path) for path in paths}
    return {path: ts for path, ts in entries.items() if path not in drop}


def load(path: Any) -> dict[str, str]:
    """读台账（文件不存在 / 坏 JSON 都回落空账）。"""
    try:
        return normalize(storage.read_json(path, {}))
    except Exception:  # noqa: BLE001 - 台账坏了不能连累语音发送
        return {}


def save(path: Any, entries: dict[str, str]) -> None:
    """原子落盘；失败抛给调用方记日志（调用方必须吞掉，不能带崩巡检）。"""
    storage.atomic_write_json(path, normalize(entries))


__all__ = [
    "LEDGER_FILENAME",
    "MAX_DELETES_PER_SWEEP",
    "MAX_ENTRIES_DEFAULT",
    "MAX_ENTRIES_LIMIT",
    "MIN_KEEP_SECONDS_DEFAULT",
    "RETENTION_HOURS_DEFAULT",
    "RETENTION_HOURS_LIMIT",
    "clamp_max_entries",
    "clamp_retention_hours",
    "load",
    "normalize",
    "plan_cleanup",
    "prune",
    "record",
    "safe_owned_path",
    "save",
]
