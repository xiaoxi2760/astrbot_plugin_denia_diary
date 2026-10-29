"""语音合成的**有界执行**（F1 第二步）：给 ``plan`` 的决策结果上一圈护栏。

``plan.py`` 只回答"转不转、转哪些"；这里回答"**怎么等**"——宿主 TTS provider 是
插件管不到的黑盒（网络调用，可能挂死、可能抛、可能返回空气），所以每一段合成
都必须有界：单段超时 + 整轮预算 + 分段数量上限，任何一路出界都**保持该段原文**。

分工与红线（与 ``plan.py`` 一致）：这里只编排 asyncio，不 import astrbot；provider
由调用方注入（离线测试用假 provider）。文件在不在是 IO，通过 ``verify_path``
回调注入——默认不查（调用方没有文件系统时也不炸）。

取消边界：``asyncio.CancelledError`` **原样上抛**。调用方（宿主钩子）被取消时，
合成必须跟着死，绝不能吞掉取消信号假装合成成功。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

MAX_SEGMENTS_DEFAULT = 3
"""一轮主动消息最多转几段（待调）：她一条消息被拆成十段语音读出来很怪，多余的段保持文字。"""

PER_CALL_TIMEOUT_DEFAULT = 20.0
"""单段合成的最长等待（秒，待调）：宿主 provider 自己也可能挂死，不能陪它挂。"""

TOTAL_BUDGET_DEFAULT = 60.0
"""整轮合成的总预算（秒，待调）：超了就放弃剩下的段——发送前钩子卡太久，她的消息会迟到。"""

MAX_SEGMENTS_LIMIT = (1, 8)
PER_CALL_TIMEOUT_LIMIT = (3.0, 120.0)
TOTAL_BUDGET_LIMIT = (10.0, 600.0)
"""配置夹紧边界：面板 / 配置层传进来的值先过这里，坏值不会变成"无限等"。"""

CANCELLATION_GRACE_SECONDS = 0.05
"""超时后给 provider 一个很短的取消收尾窗口，绝不无限等待其取消处理。"""

_BACKGROUND_TASKS: set[asyncio.Future[Any]] = set()
_BACKGROUND_BY_OWNER: dict[int, tuple[Any, asyncio.Future[Any]]] = {}


@dataclass(frozen=True)
class BoundedAwaitResult:
    """有界 await 的结果；仍在运行表示调用方不得重叠启动同一 provider。"""

    completed: bool
    value: Any = None
    timed_out: bool = False
    still_running: bool = False


def _retain_and_reap(task: asyncio.Future[Any], owner: Any = None) -> None:
    """让已脱离调用方的任务活到结束，并取走其结果/异常。"""
    _BACKGROUND_TASKS.add(task)
    if owner is not None:
        _BACKGROUND_BY_OWNER[id(owner)] = (owner, task)

    def consume(done: asyncio.Future[Any]) -> None:
        _BACKGROUND_TASKS.discard(done)
        if owner is not None:
            owner_task = _BACKGROUND_BY_OWNER.get(id(owner))
            if owner_task is not None and owner_task[0] is owner and owner_task[1] is done:
                _BACKGROUND_BY_OWNER.pop(id(owner), None)
        try:
            done.result()
        except BaseException:  # noqa: BLE001 - orphaned provider outcome has no caller
            pass

    task.add_done_callback(consume)


async def bounded_await(
    awaitable: Awaitable[Any],
    timeout: float,
    *,
    cancellation_grace: float = CANCELLATION_GRACE_SECONDS,
    owner: Any = None,
) -> BoundedAwaitResult:
    """有界等待一个 awaitable，取消调用方时尽快上抛并安全回收子任务。

    ``owner`` 可传 provider 实例：若该实例之前的调用仍在后台运行，则不启动此
    awaitable，并返回 ``still_running=True``。结果为 True 时，调用方也应停止启动
    同一 provider 的后续请求，避免取消被吞后出现重叠调用。
    """
    if owner is not None:
        owner_task = _BACKGROUND_BY_OWNER.get(id(owner))
        if owner_task is not None and owner_task[0] is owner:
            if not owner_task[1].done():
                # The common caller passes an unstarted coroutine expression; close it so
                # a rejected duplicate call cannot emit an unawaited-coroutine warning.
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                await asyncio.sleep(0)  # preserve prompt host cancellation delivery
                return BoundedAwaitResult(completed=False, still_running=True)
            _BACKGROUND_BY_OWNER.pop(id(owner), None)

    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        seconds = 0.0
    if not math.isfinite(seconds):
        seconds = 0.0
    seconds = max(0.0, seconds)
    try:
        grace = float(cancellation_grace)
    except (TypeError, ValueError):
        grace = 0.0
    if not math.isfinite(grace):
        grace = CANCELLATION_GRACE_SECONDS
    grace = max(0.0, grace)

    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        task.cancel()
        _retain_and_reap(task, owner)
        raise

    if task in done:
        return BoundedAwaitResult(completed=True, value=task.result())

    task.cancel()
    _retain_and_reap(task, owner)
    try:
        await asyncio.wait({task}, timeout=grace)
    except asyncio.CancelledError:
        # The orphan callback already owns this task; propagate host cancellation now.
        raise
    return BoundedAwaitResult(
        completed=False,
        timed_out=True,
        still_running=not task.done(),
    )


def clamp_count(value: Any, default: int = MAX_SEGMENTS_DEFAULT) -> int:
    """分段数量上限：非法回落默认，越界夹紧。"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(MAX_SEGMENTS_LIMIT[0], min(MAX_SEGMENTS_LIMIT[1], num))


def clamp_seconds(value: Any, default: float, limits: tuple[float, float]) -> float:
    """秒数边界：非法回落默认，越界夹紧到 limits。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return max(limits[0], min(limits[1], num))


def clamp_config(cfg: Any) -> dict[str, float | int]:
    """从语音配置组里取边界值（缺键 / 坏值一律回落默认并夹紧）。"""
    raw = cfg if isinstance(cfg, dict) else {}
    return {
        "max_segments": clamp_count(raw.get("max_segments")),
        "per_call_timeout": clamp_seconds(
            raw.get("tts_timeout_seconds"), PER_CALL_TIMEOUT_DEFAULT, PER_CALL_TIMEOUT_LIMIT
        ),
        "total_budget": clamp_seconds(
            raw.get("tts_total_seconds"), TOTAL_BUDGET_DEFAULT, TOTAL_BUDGET_LIMIT
        ),
    }


async def synthesize_segments(
    provider: Any,
    targets: list[tuple[int, str]],
    *,
    max_segments: int = MAX_SEGMENTS_DEFAULT,
    per_call_timeout: float = PER_CALL_TIMEOUT_DEFAULT,
    total_budget: float = TOTAL_BUDGET_DEFAULT,
    verify_path: Callable[[str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[int, str], dict[int, str]]:
    """逐段调用宿主 provider 合成，**全程有界**。

    Args:
        provider: 宿主 TTS provider（只需有 ``async get_audio(text) -> str``）。
        targets: ``[(消息下标, 文本), ...]``——**顺序即尝试顺序**（超预算时靠后的
            段先被放弃），调用方用 ``plan.speakable_indices`` 的顺序传入。
        max_segments: 最多尝试几段，超出的直接判"超出分段上限"（不调 provider）。
        per_call_timeout: 单段 ``get_audio`` 的最长等待（秒）。
        total_budget: 整轮总预算（秒）：剩余预算不够下一段时，后面的段全部放弃。
        verify_path: 合成结果的落地校验（存在且非空才收）；``None`` 表示不查。
        clock: 单调时钟（测试注入）。

    Returns:
        ``(audio, failures)``——``audio`` 是 ``{下标: 音频路径}``（只有完全成功的段），
        ``failures`` 是 ``{下标: 可读原因}``（含"根本没尝试"的段）。两个都不含的
        下标不存在——``targets`` 里的每个下标恰好落进其中一边。

    Raises:
        ``asyncio.CancelledError`` 原样上抛（取消边界：宿主取消钩子时合成跟着死）。
    """
    audio: dict[int, str] = {}
    failures: dict[int, str] = {}
    if provider is None:
        return audio, {index: "没有可用的语音合成提供方" for index, _ in targets}
    cap = max(0, int(max_segments))
    started = clock()
    provider_still_running = False
    for order, (index, text) in enumerate(targets):
        if order >= cap:
            failures[index] = f"超出单轮分段上限（{cap} 段），本段保持文字"
            continue
        elapsed = clock() - started
        remaining = float(total_budget) - elapsed
        if remaining <= 0:
            failures[index] = (
                f"本轮语音合成总预算已用完（{float(total_budget):g} 秒），本段保持文字"
            )
            continue
        if provider_still_running:
            failures[index] = "上一段语音合成超时后仍未停止，本段跳过以避免并发调用 provider"
            continue
        effective_timeout = max(0.05, min(float(per_call_timeout), remaining))
        try:
            result = await bounded_await(provider.get_audio(text), effective_timeout, owner=provider)
        except asyncio.CancelledError:
            raise  # 取消边界：绝不吞
        except Exception as error:  # noqa: BLE001 - 单段失败不连累其余
            failures[index] = f"语音合成失败：{error}"
            continue
        if result.still_running and not result.timed_out:
            failures[index] = "语音合成 provider 仍有未结束调用，本段跳过以避免并发调用"
            provider_still_running = True
            continue
        if result.timed_out:
            failures[index] = f"语音合成超时（等待 {effective_timeout:g} 秒无结果），本段保持文字"
            provider_still_running = result.still_running
            continue
        path = result.value
        path_text = str(path or "").strip()
        if not path_text:
            failures[index] = "语音合成没有返回音频文件，本段保持文字"
            continue
        if verify_path is not None and not verify_path(path_text):
            failures[index] = "合成的音频文件不存在或为空，本段保持文字"
            continue
        audio[index] = path_text
    return audio, failures


__all__ = [
    "MAX_SEGMENTS_DEFAULT",
    "MAX_SEGMENTS_LIMIT",
    "PER_CALL_TIMEOUT_DEFAULT",
    "PER_CALL_TIMEOUT_LIMIT",
    "TOTAL_BUDGET_DEFAULT",
    "TOTAL_BUDGET_LIMIT",
    "BoundedAwaitResult",
    "bounded_await",
    "clamp_config",
    "clamp_count",
    "clamp_seconds",
    "synthesize_segments",
]
