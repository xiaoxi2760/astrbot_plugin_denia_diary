"""生图提供方适配层（任务 2 第 1 条）：**两个真实协议的适配器**，不做通用工作流。

明确支持的两种协议（其余一律不冒充）：

1. **A1111（本机 Stable Diffusion WebUI API）**
   - ``POST {base_url}/sdapi/v1/txt2img``；带参考图时改走 ``/sdapi/v1/img2img``
     （``init_images`` + ``denoising_strength``）——这是它的正式接口，不是编的；
   - 200 响应形如 ``{"images": ["<base64>"], "info": "..."}``；
   - 支持 seed / 自定义宽高 / negative prompt / 参考图（人物一致性较强）。
2. **OpenAI Images（云端，OpenAI 兼容 ``/v1/images/generations``）**
   - ``POST {base_url}/images/generations``，``Authorization: Bearer <key>``；
   - 200 响应形如 ``{"created": ..., "data": [{"b64_json": "..."}]}``；
   - **只接受内联图片**（``b64_json``）：服务端只回 URL 时不偷偷下载——把响应里
     未约束的 URL 拿去取回属于任务书禁止的"未经约束的 URL 处理"；
   - 不支持参考图 / seed（如实声明，请求体里**绝不出现**这两个字段）；
     ``response_format`` 参数对 ``gpt-image-*`` 是未知参数（会 400），对
     dall-e 系与多数兼容网关是必需——按模型名区分，这一差异如实写进能力说明。

适配器是**纯构建/解析**：HTTP 由调用方注入的 transport 执行（生产走
``transport.urllib_transport``，测试换成桩），因此离线可测、不发真请求。

**密钥纪律**：密钥只从 ``api_key_env`` 指定的环境变量读取、只进 ``Authorization``
头；不进日志、不进异常文本、不进缓存、不进提示词。
"""

from __future__ import annotations

import base64
import binascii
import json
from urllib.parse import urlsplit
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PROVIDER_CHOICES: tuple[str, ...] = ("a1111", "openai")
"""配置 ``imagery.provider`` 的合法值（与 ``core.settings.FIELD_CHOICES`` 对齐）。"""

PROVIDER_LABELS: dict[str, str] = {
    "a1111": "本机 Stable Diffusion WebUI（A1111 兼容 API）",
    "openai": "OpenAI 兼容 Images 接口（云端）",
}

MAX_IMAGE_BASE64_CHARS = 20 * 1024 * 1024
"""内联 base64 图片的编码长度上限；先检查再分配解码结果。"""


class ProviderError(Exception):
    """适配层失败（构建 / 解析 / 协议不符）。文案是给人看的，**不含密钥与请求头**。"""


@dataclass(frozen=True)
class Capabilities:
    """能力声明：**它说什么就是什么**，UI 与门面都按它说话，不许一层层加戏。"""

    reference_image: bool
    """支持参考图（图生图）。False 时适配器不会构造参考图字段，一致性只能靠文字锚定。"""
    seed: bool
    negative_prompt: bool
    size_mode: str
    """"w+h"（自定义宽高）｜"enum"（服务端枚举的 size 字符串）｜"none"。"""
    auth_required: bool
    consistency_note: str
    """人物一致性的如实说明（进面板，不改写）。"""


@dataclass(frozen=True)
class ImageRequest:
    """一次生图请求的**语义**参数（与协议无关）。"""

    prompt: str
    negative_prompt: str = ""
    width: int = 512
    height: int = 512
    size: str = "512x512"
    """服务端枚举型尺寸（OpenAI 系用原样字符串；A1111 用 width/height）。"""
    steps: int = 20
    seed: int = -1
    denoising: float = 0.6
    """参考图重绘幅度 0~1（A1111 img2img）。"""
    reference_bytes: bytes | None = None
    reference_mime: str = ""
    timeout: float = 90.0


@dataclass(frozen=True)
class HttpRequest:
    """transport 层的 HTTP 请求（协议已定型，密钥已在头里）。"""

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    timeout: float = 90.0


@dataclass(frozen=True)
class HttpResponse:
    """transport 层的 HTTP 响应。"""

    status: int
    body: bytes = b""
    content_type: str = ""


Transport = Callable[[HttpRequest], HttpResponse]


def parse_size(text: str) -> tuple[int, int]:
    """``"512x512"`` → ``(512, 512)``；格式不对抛 ``ProviderError``（A1111 需要宽高）。"""
    parts = str(text or "").lower().split("x", 1)
    if len(parts) != 2:
        raise ProviderError("尺寸要用「宽x高」格式，例如 512x512")
    try:
        width, height = int(parts[0].strip()), int(parts[1].strip())
    except ValueError as exc:
        raise ProviderError("尺寸要用「宽x高」格式，例如 512x512") from exc
    if not (64 <= width <= 2048 and 64 <= height <= 2048):
        raise ProviderError("尺寸须在 64~2048 之间")
    return width, height


def _validated_base_url(value: str, provider: str) -> str:
    """严格验证 HTTP(S) 服务根地址，允许 localhost 和本机 IP。"""
    base_url = str(value or "").strip().rstrip("/")
    if not base_url:
        raise ProviderError(f"{provider} 还没配置服务地址（imagery.base_url）")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in base_url):
        raise ProviderError(f"{provider} 服务地址格式无效")
    try:
        parsed = urlsplit(base_url)
        # Accessing .port performs urllib's numeric/range validation and can raise ValueError.
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(f"{provider} 服务地址格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ProviderError(f"{provider} 服务地址只允许 http:// 或 https://")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ProviderError(f"{provider} 服务地址须包含主机名且不能包含用户名或密码")
    if "?" in base_url or "#" in base_url or parsed.netloc.endswith(":"):
        raise ProviderError(f"{provider} 服务地址不能包含 query、fragment 或无效端口")
    if port is not None and not (1 <= port <= 65535):
        raise ProviderError(f"{provider} 服务地址端口无效")
    return base_url


def resolve_key(key_env: str) -> str:
    """环境变量**名** → 密钥值。只在这里碰 ``os.environ``；名字为空回空串。"""
    import os

    name = str(key_env or "").strip()
    if not name:
        return ""
    return os.environ.get(name, "")


def _b64_of(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _strip_data_url(text: str) -> str:
    """剥掉 ``data:image/png;base64,`` 前缀（部分 A1111 fork 会带）。"""
    if text.startswith("data:"):
        _head, _sep, tail = text.partition(",")
        return tail
    return text


def _error_detail(resp: HttpResponse) -> str:
    """返回固定类别；绝不把可能含密钥的远端错误 body 回显给 UI 或日志。"""
    return "远端服务返回错误响应"


def _decode_image_payload(text: str, provider: str) -> bytes:
    text = _strip_data_url(str(text or "").strip())
    if not text:
        raise ProviderError(f"{provider} 返回了空的图片数据")
    if len(text) > MAX_IMAGE_BASE64_CHARS:
        raise ProviderError(f"{provider} 返回的图片编码超过 20 MB 上限")
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderError(f"{provider} 返回的图片数据不是有效 base64") from exc


class BaseProvider:
    """适配器接口：``build_request`` / ``parse_response`` 都是纯函数。"""

    id: str = ""
    label: str = ""
    capabilities: Capabilities

    def build_request(self, req: ImageRequest, api_key: str) -> HttpRequest:
        raise NotImplementedError

    def parse_response(self, resp: HttpResponse) -> tuple[bytes, str]:
        raise NotImplementedError


class A1111Provider(BaseProvider):
    """本机 Stable Diffusion WebUI（A1111 兼容）。txt2img / img2img。"""

    id = "a1111"
    label = PROVIDER_LABELS["a1111"]
    capabilities = Capabilities(
        reference_image=True,
        seed=True,
        negative_prompt=True,
        size_mode="w+h",
        auth_required=False,
        consistency_note=(
            "支持使用当前立绘作为参考图；人物一致性受模型和重绘幅度影响，需结合实际出图测试。"
            "关闭参考图时只靠文字锚定。"
        ),
    )

    def __init__(self, base_url: str, model: str = "") -> None:
        self.base_url = _validated_base_url(base_url, "A1111")
        self.model = str(model or "").strip()

    def build_request(self, req: ImageRequest, api_key: str) -> HttpRequest:
        width, height = parse_size(req.size)
        payload: dict[str, Any] = {
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt,
            "steps": int(req.steps),
            "width": width,
            "height": height,
            "seed": int(req.seed),
            "sampler_index": "Euler a",
        }
        if self.model:
            payload["override_settings"] = {"sd_model_checkpoint": self.model}
            payload["override_settings_restore_afterwards"] = True
        endpoint = "txt2img"
        if req.reference_bytes is not None:
            endpoint = "img2img"
            payload["init_images"] = [_b64_of(req.reference_bytes)]
            payload["denoising_strength"] = max(0.0, min(1.0, float(req.denoising)))
            payload["resize_mode"] = 1
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return HttpRequest(
            url=f"{self.base_url}/sdapi/v1/{endpoint}",
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=req.timeout,
        )

    def parse_response(self, resp: HttpResponse) -> tuple[bytes, str]:
        if resp.status != 200:
            raise ProviderError(f"A1111 出图失败（HTTP {resp.status}）：{_error_detail(resp)}")
        _require_json_content_type(resp, "A1111")
        try:
            doc = json.loads(resp.body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("A1111 的响应不是有效 JSON") from exc
        images = doc.get("images") if isinstance(doc, dict) else None
        if not isinstance(images, list) or not images:
            raise ProviderError("A1111 的响应里没有图片（images 为空）")
        return _decode_image_payload(str(images[0]), "A1111"), "image/png"


class OpenAIImagesProvider(BaseProvider):
    """OpenAI 兼容 Images 接口（云端）。只做 ``/images/generations``，只收 ``b64_json``。"""

    id = "openai"
    label = PROVIDER_LABELS["openai"]
    capabilities = Capabilities(
        reference_image=False,
        seed=False,
        negative_prompt=False,
        size_mode="enum",
        auth_required=True,
        consistency_note=(
            "这个接口不支持参考图与 seed：人物一致性只能靠文字描述锚定，较弱；"
            "界面上的参考图 / 随机种子选项对它无效（不会假装生效）。"
        ),
    )

    def __init__(self, base_url: str, model: str = "") -> None:
        self.base_url = _validated_base_url(base_url, "OpenAI 兼容接口")
        self.model = str(model or "").strip()

    @staticmethod
    def _accepts_response_format(model: str) -> bool:
        """``gpt-image-*`` 不接受 ``response_format``（总是回 b64_json）；dall-e 系需要它。"""
        return not model.lower().startswith("gpt-image")

    def build_request(self, req: ImageRequest, api_key: str) -> HttpRequest:
        if not self.model:
            raise ProviderError("还没配置生图模型（imagery.model）")
        if not api_key:
            raise ProviderError("这个接口需要密钥：在配置里填环境变量名（imagery.api_key_env），并确保宿主环境里已设置")
        model = self.model
        payload: dict[str, Any] = {
            "model": model,
            "prompt": req.prompt,
            "n": 1,
            "size": req.size,
        }
        if self._accepts_response_format(model):
            payload["response_format"] = "b64_json"
        # 能力声明为 False 的东西（参考图 / seed / negative）在这里**根本没有字段**：
        # 不是"传了但忽略"，是压根不发——测试会逐字段核对。
        return HttpRequest(
            url=f"{self.base_url}/images/generations",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=req.timeout,
        )

    def parse_response(self, resp: HttpResponse) -> tuple[bytes, str]:
        if resp.status != 200:
            raise ProviderError(f"生图接口失败（HTTP {resp.status}）：{_error_detail(resp)}")
        _require_json_content_type(resp, "生图接口")
        try:
            doc = json.loads(resp.body.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderError("生图接口的响应不是有效 JSON") from exc
        data = doc.get("data") if isinstance(doc, dict) else None
        item = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if item is None:
            raise ProviderError("生图接口的响应里没有图片数据（data 为空）")
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64.strip():
            return _decode_image_payload(b64, "生图接口"), "image/png"
        # 只有 URL：如实拒绝，不去下载未约束的地址。
        raise ProviderError(
            "该服务只回了图片链接、没有内联图片（b64_json）。"
            "本适配器只接受内联图片，不会下载未约束的 URL；请换支持 b64_json 的模型或网关。"
        )


def build_provider(provider_id: str, *, base_url: str, model: str) -> BaseProvider:
    """配置 → 适配器实例。``provider`` 值不认识就报错（不静默回落——生图是要花钱的）。"""
    text = str(provider_id or "").strip()
    if text == "a1111":
        return A1111Provider(base_url, model)
    if text == "openai":
        return OpenAIImagesProvider(base_url, model)
    raise ProviderError(f"未知的生图提供方「{text}」，可选：{'、'.join(PROVIDER_CHOICES)}")


def _require_json_content_type(resp: HttpResponse, provider: str) -> None:
    """若服务声明了类型，拒绝明显不是 JSON 的成功响应。"""
    content_type = str(resp.content_type or "").split(";", 1)[0].strip().lower()
    if content_type and content_type != "application/json" and not (
        content_type.startswith("application/") and content_type.endswith("+json")
    ):
        raise ProviderError(f"{provider} 返回了非 JSON 响应，无法解析图片")


__all__ = [
    "PROVIDER_CHOICES",
    "PROVIDER_LABELS",
    "A1111Provider",
    "Capabilities",
    "HttpRequest",
    "HttpResponse",
    "ImageRequest",
    "OpenAIImagesProvider",
    "ProviderError",
    "Transport",
    "build_provider",
    "parse_size",
    "resolve_key",
]
