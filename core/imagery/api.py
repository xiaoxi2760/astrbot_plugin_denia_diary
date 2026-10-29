"""生图门面（任务 2 核心）：开关闸门 → 提示词 → 缓存 → 配额 → 适配器 → 落盘。

**编排住在这里，判断不外泄**——``main.py`` 只递实例，``web_api`` 只递请求；
全部闸门在 ``generate`` 里按序落：

1. **开关**（``imagery.enabled``，默认关）与调用方可信度：不过闸**不发任何请求**；
2. **并发**：同门面同时只有一张在画（内存锁），后来的直接拒绝——排队白等不如明说；
3. **提示词**：``prompt.build_scene`` 只吃公开日常（生活计划当前项 / 作息词 / 情绪词
   + 主人手填锚定），私聊日记与私人记忆不进参数；
4. **缓存**：全部影响画面的要素（提示词、模型、参数、参与生成的立绘指纹）合成
   canonical JSON → ``sha256``。命中即返还，**不 spending、不发请求**——无关页面
   刷新不会重复调用；
5. **配额**：今日次数用完直接拒（请求未发出，不计数）；计数发生在 HTTP 发出**之前**
   （``quota.spend``），超时 / 失败 / 坏图一律**不返还**——云端已计费，返还等于
   允许超时无限重试；
6. **适配器**：按配置现建（热生效零成本），构建失败发生在花钱之前；
7. **收尾**：Pillow 完整解码拒绝坏图与超限图，缓存落盘失败降级为就地 data_url
   返还；超时或取消后，底层请求实际结束前仍保持忙碌，迟到结果不发布。

依赖标准库、同包模块与 Pillow；不 import astrbot。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import uuid
from datetime import datetime
from typing import Any

from .. import storage
from ..webui_portrait import PortraitError, data_url_of
from ..state.api import MOOD_TTL
from . import prompt as prompt_mod
from .cache import MAX_IMAGE_BYTES, ImageCache, key_for
from .validation import ensure_validator, validate_image
from .providers import (
    A1111Provider,
    Capabilities,
    ImageRequest,
    OpenAIImagesProvider,
    ProviderError,
    build_provider,
    resolve_key,
)
from .quota import DailyQuota
from .transport import urllib_transport

_CAPABILITIES: dict[str, Capabilities] = {
    "a1111": A1111Provider.capabilities,
    "openai": OpenAIImagesProvider.capabilities,
}
"""provider id → 静态能力声明（状态页用，**不构造实例**——构造要校验 base_url，
状态页不该因为没配地址就报错）。"""

_KEY_VERSION = 2
"""缓存键版本：键语义变了就 +1，旧缓存整体自然失效。"""


def capabilities_of(provider_id: str) -> Capabilities | None:
    return _CAPABILITIES.get(str(provider_id or "").strip())


class Imagery:
    """状态生图门面。依赖全部注入（settings 引用可被 ``apply_settings`` 热替换）。"""

    def __init__(
        self,
        settings: Any,
        layout: Any,
        locks: Any,
        portraits: Any,
        life: Any,
        state: Any,
        *,
        transport: Any = None,
    ) -> None:
        self.settings = settings
        self.layout = layout
        self.locks = locks
        self.portraits = portraits
        self.life = life
        self.state = state
        self._transport = transport or urllib_transport
        self._cache = ImageCache(layout.image_cache)
        self._quota = DailyQuota(layout.imagery_quota, locks)
        self._busy = asyncio.Lock()
        self._transport_task: asyncio.Task[Any] | None = None
        self._cache.cleanup_tmp()  # 上次进程死在生成中途的 .part 残骸，启动即收

    # ---- 配置 -------------------------------------------------------------------

    def _cfg(self) -> dict[str, Any]:
        return dict(getattr(self.settings, "imagery", {}) or {})

    def _now(self, now: datetime | None) -> datetime:
        if isinstance(now, datetime):
            return now
        zone = getattr(self.settings, "zone", None)
        return datetime.now(zone() if callable(zone) else None) if zone else datetime.now().astimezone()

    # ---- 只读 -------------------------------------------------------------------

    def status_payload(self, *, now: datetime | None = None) -> dict[str, Any]:
        """面板状态页：配置、能力、今日用量、最近一张的元信息。**纯只读**——不发请求、
        不计数、不触发任何生成。"""
        cfg = self._cfg()
        now = self._now(now)
        provider_id = str(cfg.get("provider") or "")
        caps = capabilities_of(provider_id)
        quota = self._quota.state(now=now)
        limit = int(cfg.get("daily_limit") or 0)
        latest = self._latest_meta()
        key_env = str(cfg.get("api_key_env") or "")
        payload: dict[str, Any] = {
            "ok": True,
            "enabled": bool(cfg.get("enabled")),
            "provider": provider_id,
            "provider_label": provider_id,
            "model": str(cfg.get("model") or ""),
            "base_url": str(cfg.get("base_url") or ""),
            "api_key_env": key_env,
            "api_key_present": bool(resolve_key(key_env)),
            "size": str(cfg.get("size") or ""),
            "steps": int(cfg.get("steps") or 0),
            "seed": int(cfg.get("seed") if cfg.get("seed") is not None else -1),
            "denoising": int(cfg.get("denoising") or 0),
            "use_reference": bool(cfg.get("use_reference")),
            "timeout_seconds": int(cfg.get("timeout_seconds") or 0),
            "character_hint": str(cfg.get("character_hint") or ""),
            "style_hint": str(cfg.get("style_hint") or ""),
            "capabilities": self._capabilities_view(caps),
            "reference_ready": self._has_portrait(),
            "daily": {
                "date": quota["date"],
                "used": quota["used"],
                "limit": limit,
                **({"error": quota["error"]} if quota.get("error") else {}),
            },
            "cache_reuse_supported": not (provider_id == "a1111" and not str(cfg.get("model") or "").strip()),
            "latest": latest,
        }
        if provider_id in {"a1111", "openai"}:
            payload["provider_label"] = self._provider_label(provider_id)
        if caps is not None and not caps.reference_image and bool(cfg.get("use_reference")):
            payload["notice"] = "当前提供方不支持参考图：界面上的立绘参考不会生效（不会假装生效）。"
        return payload

    def image_payload(self) -> dict[str, Any]:
        """最近一张生图（``data_url``，受限 iframe 里唯一可靠的图片通道，与立绘同款）。"""
        hit = self._latest_hit()
        if hit is None:
            return {"ok": False, "error": "还没有生成过图。点「画一下现在的她」试试。"}
        key, meta, path = hit
        try:
            data = path.read_bytes()
        except OSError:
            return {"ok": False, "error": "图片文件读不到了（可能被移动或删除）。"}
        return {
            "ok": True,
            "image": {
                "key": key,
                "data_url": data_url_of(str(meta.get("mime") or "image/png"), data),
                "mime": str(meta.get("mime") or ""),
                "created_at": str(meta.get("created_at") or ""),
                "prompt": str(meta.get("prompt") or ""),
                "provider": str(meta.get("provider") or ""),
                "model": str(meta.get("model") or ""),
            },
        }

    # ---- 生成 -------------------------------------------------------------------

    async def generate(self, *, now: datetime | None = None, trusted: bool = False) -> dict[str, Any]:
        """按需画一张「现在的她」。只许面板（``trusted=True``）触发：本功能没有、
        也不许有聊天侧的自动触发路径。"""
        cfg = self._cfg()
        now = self._now(now)
        daily = self._daily_view(now)
        if not trusted:
            return {"ok": False, "error": "生图只在面板按需触发，聊天里不画。"}
        if not bool(cfg.get("enabled")):
            return {"ok": False, "error": "状态生图当前是关闭的（设置 → 生活 → 状态生图）。", "daily": daily}
        if self._transport_active():
            return {"ok": False, "conflict": True, "error": "上一张请求的传输仍在运行，等它结束再点。", "daily": daily}
        if self._busy.locked():
            return {"ok": False, "conflict": True, "error": "已经有一张在画了，等它完成再点。", "daily": daily}

        async with self._busy:
            if self._transport_active():
                return {"ok": False, "conflict": True, "error": "上一张请求的传输仍在运行，等它结束再点。", "daily": daily}
            return await self._generate_locked(cfg, now)

    def _transport_active(self) -> bool:
        task = self._transport_task
        if task is None:
            return False
        if task.done():
            self._transport_task = None
            return False
        return True

    def _transport_finished(self, task: asyncio.Task[Any]) -> None:
        if self._transport_task is task:
            self._transport_task = None
        if not task.cancelled():
            try:
                task.exception()
            except BaseException:  # noqa: BLE001 - done callback must not leak errors
                pass

    async def _generate_locked(self, cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
        quota = self._quota.state(now=now)
        if quota.get("error"):
            return {"ok": False, "error": quota["error"], "daily": self._daily_view(now)}
        scene = self._scene(cfg, now)
        reference = await self._reference_bytes(cfg)
        key_payload = self._key_payload(cfg, scene, reference)
        cache_reuse_supported = not (
            str(cfg.get("provider") or "") == "a1111" and not str(cfg.get("model") or "").strip()
        )
        if not cache_reuse_supported:
            key_payload["unverified_model_run"] = uuid.uuid4().hex
        key = key_for(key_payload)

        cached = self._cache.find(key) if cache_reuse_supported else None
        if cached is not None:
            meta = self._cache.read_meta(key) or {}
            return {
                "ok": True,
                "cached": True,
                "image": self._image_view(key, meta, cached),
                "daily": self._daily_view(now),
                "notice": ["与最近一次相同（命中缓存，没有再请求模型）。"],
            }

        limit = int(cfg.get("daily_limit") or 0)
        quota = self._quota.state(now=now)
        if quota.get("error"):
            return {"ok": False, "error": quota["error"], "daily": self._daily_view(now)}
        if quota["used"] >= limit:
            return {
                "ok": False,
                "error": f"今日生图次数已用完（{quota['used']}/{limit}）。明天再来，或在设置里调每日上限。",
                "daily": self._daily_view(now),
            }

        provider_id = str(cfg.get("provider") or "")
        caps = capabilities_of(provider_id)
        timeout = float(cfg.get("timeout_seconds") or 90)
        request = ImageRequest(
            prompt=scene["prompt"],
            negative_prompt=scene["negative_prompt"] if caps and caps.negative_prompt else "",
            size=str(cfg.get("size") or ""),
            steps=int(cfg.get("steps") or 20),
            seed=int(cfg.get("seed") if cfg.get("seed") is not None else -1),
            denoising=(int(cfg.get("denoising") or 60) / 100.0),
            reference_bytes=reference["bytes"],
            reference_mime=reference["mime"],
            timeout=timeout,
        )
        try:
            provider = build_provider(provider_id, base_url=str(cfg.get("base_url") or ""), model=str(cfg.get("model") or ""))
            api_key = resolve_key(str(cfg.get("api_key_env") or ""))
            http_request = provider.build_request(request, api_key)
        except ProviderError as error:
            # 配置问题：请求**还没有发出**，不花费、不计数。
            return {"ok": False, "error": str(error), "daily": self._daily_view(now)}

        try:
            ensure_validator()
        except PortraitError as error:
            return {"ok": False, "error": str(error), "daily": self._daily_view(now)}

        # ── 从这里开始请求即将发出：先记账，再发送；此后任何失败都不返还 ──
        try:
            await self._quota.spend(result="sent", detail=f"{provider_id} {cfg.get('model') or ''}".strip(), now=now)
        except (OSError, ValueError) as error:
            return {"ok": False, "error": f"生图次数记账失败，本次不发请求：{error}", "daily": self._daily_view(now)}

        notices: list[str] = list(reference["notices"])
        transport_task = asyncio.create_task(asyncio.to_thread(self._transport, http_request))
        self._transport_task = transport_task
        transport_task.add_done_callback(self._transport_finished)
        try:
            response = await asyncio.wait_for(asyncio.shield(transport_task), timeout=timeout)
            raw, mime = provider.parse_response(response)
        except asyncio.TimeoutError:
            await self._note("timeout", f"{provider_id} 超时（{timeout:g}s）", now=now)
            return {
                "ok": False,
                "error": f"生图超时（{timeout:g} 秒），已计入今日用量；配额不返还。",
                "daily": self._daily_view(now),
            }
        except asyncio.CancelledError:
            await self._note("cancelled", f"{provider_id} 已取消（请求可能已发出）", now=now)
            raise
        except ProviderError as error:
            await self._note("failed", f"{provider_id}：{str(error)[:60]}", now=now)
            return {"ok": False, "error": str(error), "daily": self._daily_view(now)}

        try:
            mime, ext = validate_image(raw)
        except PortraitError as error:
            await self._note("bad_image", str(error), now=now)
            return {
                "ok": False,
                "error": f"服务返回的图片不可用：{error} 已计入今日用量。",
                "daily": self._daily_view(now),
            }
        if len(raw) > MAX_IMAGE_BYTES:
            await self._note("bad_image", f"图片过大（{len(raw)} 字节）", now=now)
            return {
                "ok": False,
                "error": f"返回图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB 上限，已计入今日用量。",
                "daily": self._daily_view(now),
            }

        meta = {
            "key": key,
            "created_at": now.isoformat(timespec="seconds"),
            "provider": provider_id,
            "model": str(cfg.get("model") or ""),
            "prompt": scene["prompt"],
            "base_url": str(cfg.get("base_url") or "").rstrip("/"),
            "size": str(cfg.get("size") or ""),
            "mime": mime,
            "reference_used": reference["bytes"] is not None,
            "reference_id": reference["id"],
        }
        try:
            path = self._cache.store(key, raw, ext, meta)
        except (OSError, ValueError) as error:
            # 缓存写不进去：图已经生成、钱已经花了——就地以 data_url 返还，不浪费这一次。
            notices.append(f"缓存写入失败（{type(error).__name__}），这张图只在本次响应里、不会留存。")
            await self._note("ok", "生成成功（缓存写入失败，未留存）", now=now)
            return {
                "ok": True,
                "cached": False,
                "image": {
                    "key": key,
                    "data_url": data_url_of(mime, raw),
                    "mime": mime,
                    "created_at": meta["created_at"],
                    "prompt": meta["prompt"],
                    "provider": provider_id,
                    "model": meta["model"],
                },
                "daily": self._daily_view(now),
                "notice": notices,
            }
        await self._note("ok", f"{provider_id} 生成成功", now=now)
        return {
            "ok": True,
            "cached": False,
            "image": self._image_view(key, meta, path),
            "daily": self._daily_view(now),
            "notice": notices,
        }

    # ---- 组装件 -----------------------------------------------------------------

    def _scene(self, cfg: dict[str, Any], now: datetime) -> dict[str, str]:
        """公开日常要素收集：生活计划的当前活动、作息词、可见期内的情绪词。"""
        activity: dict[str, str] | None = None
        rhythm = ""
        mood = ""
        try:
            snap = self.life.snapshot(now=now)
            current = snap.get("current") if isinstance(snap, dict) else None
            if isinstance(current, dict) and current.get("title"):
                activity = {"title": str(current.get("title") or ""), "location": str(current.get("location") or "")}
        except Exception:  # noqa: BLE001 - 生活日程读不到不拦生图
            activity = None
        try:
            state_snap = self.state.snapshot(now=now) if self.state is not None else {}
            if isinstance(state_snap, dict):
                rhythm = str((state_snap.get("rhythm") or {}).get("word") or "")
                mood_info = state_snap.get("mood") or {}
                mood = self._fresh_mood_word(mood_info, now)
        except Exception:  # noqa: BLE001 - 状态系统关了/坏了也不拦生图
            rhythm, mood = "", ""
        return prompt_mod.build_scene(
            {
                "character_hint": str(cfg.get("character_hint") or ""),
                "style_hint": str(cfg.get("style_hint") or ""),
                "activity": activity,
                "rhythm_word": rhythm,
                "mood_word": mood,
            }
        )

    @staticmethod
    def _fresh_mood_word(mood: Any, now: datetime) -> str:
        """情绪词只在可见期内采用（与注入层同一口径：过期＝已经过去了）。"""
        if not isinstance(mood, dict):
            return ""
        word = str(mood.get("word") or "").strip()
        if not word:
            return ""
        updated = str(mood.get("word_at") or mood.get("updated_at") or "")
        try:
            moment = datetime.fromisoformat(updated)
        except ValueError:
            return ""
        if moment.tzinfo is None:
            return ""
        return word if now - moment <= MOOD_TTL else ""

    async def _reference_bytes(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """参考图收集：**提供方支持 + 配置要求 + 立绘存在**三者同时成立才参与。

        任何一环不满足都不传参考图、也不伪造——返回值带 ``notices`` 向面板如实说明。
        """
        empty = {"bytes": None, "mime": "", "id": None, "notices": []}
        caps = capabilities_of(str(cfg.get("provider") or ""))
        if caps is None or not caps.reference_image:
            return empty
        if not bool(cfg.get("use_reference")):
            return empty
        try:
            snap = await self.portraits.snapshot()
        except Exception:  # noqa: BLE001 - 立绘仓库坏了不拦生图
            snap = None
        current = (snap or {}).get("current") if isinstance(snap, dict) else None
        if not isinstance(current, dict) or not current.get("data_url"):
            if self._has_portrait():
                return {**empty, "notices": ["当前立绘读不到，这次按文字锚定画。"]}
            return {**empty, "notices": ["还没有立绘可参考，这次按文字锚定画。"]}
        _head, _sep, b64 = str(current["data_url"]).partition(",")
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            return {**empty, "notices": ["当前立绘数据损坏，这次按文字锚定画。"]}
        return {"bytes": data, "mime": str(current.get("mime") or ""), "id": str(current.get("id") or ""), "notices": []}

    def _key_payload(self, cfg: dict[str, Any], scene: dict[str, str], reference: dict[str, Any]) -> dict[str, Any]:
        """缓存键的全部要素。立绘指纹**只在参考图真的参与生成时**进键——它不参与时
        立绘变了画面也不会变，进键只会造成无意义的重画。"""
        provider_id = str(cfg.get("provider") or "")
        caps = capabilities_of(provider_id)
        payload: dict[str, Any] = {
            "v": _KEY_VERSION,
            "provider": provider_id,
            "base_url": str(cfg.get("base_url") or "").rstrip("/"),
            "model": str(cfg.get("model") or ""),
            "prompt": scene["prompt"],
            "size": str(cfg.get("size") or ""),
            "reference": None,
        }
        if caps is not None and caps.negative_prompt:
            payload["negative_prompt"] = scene["negative_prompt"]
        if provider_id == "a1111":
            payload["steps"] = int(cfg.get("steps") or 20)
        if caps is not None and caps.seed:
            payload["seed"] = int(cfg.get("seed") if cfg.get("seed") is not None else -1)
        if reference["bytes"] is not None:
            payload["denoising"] = int(cfg.get("denoising") or 60)
            payload["reference"] = {
                "id": reference["id"],
                "bytes": len(reference["bytes"]),
                "sha16": storage_stable_hash(reference["bytes"]),
            }
        return payload

    def _daily_view(self, now: datetime) -> dict[str, Any]:
        quota = self._quota.state(now=now)
        view = {"date": quota["date"], "used": quota["used"], "limit": int(self._cfg().get("daily_limit") or 0)}
        if quota.get("error"):
            view["error"] = quota["error"]
        return view

    def _latest_hit(self) -> tuple[str, dict[str, Any], Any] | None:
        return self._cache.latest()

    def _latest_meta(self) -> dict[str, Any] | None:
        hit = self._latest_hit()
        if hit is None:
            return None
        key, meta, _path = hit
        view = self._image_view(key, meta, None)
        view.pop("data_url", None)  # 状态页不带图（几十 KB 到几 MB），图走 imagery/image 单独取
        return view

    @staticmethod
    def _image_view(key: str, meta: dict[str, Any], path: Any) -> dict[str, Any]:
        view: dict[str, Any] = {
            "key": key,
            "mime": str(meta.get("mime") or "image/png"),
            "created_at": str(meta.get("created_at") or ""),
            "prompt": str(meta.get("prompt") or ""),
            "provider": str(meta.get("provider") or ""),
            "model": str(meta.get("model") or ""),
        }
        if path is not None:
            try:
                data = path.read_bytes()
                view["data_url"] = data_url_of(view["mime"], data)
            except OSError:
                pass
        return view

    def _has_portrait(self) -> bool:
        try:
            index = self.portraits.read_index()
        except Exception:  # noqa: BLE001
            return False
        return bool(index.get("current"))

    @staticmethod
    def _provider_label(provider_id: str) -> str:
        return {"a1111": A1111Provider.label, "openai": OpenAIImagesProvider.label}.get(provider_id, provider_id)

    @staticmethod
    def _capabilities_view(caps: Capabilities | None) -> dict[str, Any]:
        if caps is None:
            return {"known": False}
        return {
            "known": True,
            "reference_image": caps.reference_image,
            "seed": caps.seed,
            "negative_prompt": caps.negative_prompt,
            "size_mode": caps.size_mode,
            "auth_required": caps.auth_required,
            "consistency_note": caps.consistency_note,
        }

    async def _note(self, result: str, detail: str, *, now: datetime) -> None:
        """事后补一条结果留痕（best-effort：审计用，失败不影响主流程）。"""
        try:
            async with self._quota._lock():
                doc = self._quota._read()
                request_date = now.date().isoformat()
                if doc["date"] != request_date:
                    return
                events = [{"ts": now.isoformat(timespec="seconds"), "result": str(result)[:24], "detail": str(detail)[:60]}, *doc["events"]]
                storage.atomic_write_json(
                    self._quota.path,
                    {"date": doc["date"], "used": doc["used"], "events": events[:20]},
                )
        except Exception:  # noqa: BLE001 - 留痕失败不连累结果
            pass


def storage_stable_hash(data: bytes) -> str:
    """立绘内容指纹（缓存键要素）。"""
    return hashlib.sha256(data).hexdigest()[:16]


__all__ = ["Imagery", "capabilities_of"]
