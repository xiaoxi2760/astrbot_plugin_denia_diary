"""Bounded image decoding before publishing or caching provider output."""
from __future__ import annotations

import io
import warnings

from ..webui_portrait import PortraitError

MAX_PIXELS = 16 * 1024 * 1024
MAX_DIMENSION = 8192
MAX_BYTES = 20 * 1024 * 1024
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"),
           "WEBP": ("image/webp", ".webp"), "GIF": ("image/gif", ".gif")}


def ensure_validator() -> None:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        raise PortraitError("缺少 Pillow 图片校验依赖，请安装插件依赖后重试（本次不请求模型）。") from None


def validate_image(data: bytes) -> tuple[str, str]:
    ensure_validator()
    if not data or len(data) > MAX_BYTES:
        raise PortraitError("图片为空或超过 20 MB 上限。")
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                kind = image.format
                width, height = image.size
                if kind not in FORMATS or min(width, height) <= 0:
                    raise ValueError("unsupported image")
                if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
                    raise ValueError("image dimensions exceed budget")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated output is not supported")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except Exception:
        raise PortraitError("服务返回的图片不完整、无法解码，或尺寸超过限制；保留上一张结果。") from None
    return FORMATS[kind]
