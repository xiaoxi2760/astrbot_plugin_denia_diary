"""生产 transport：标准库 ``urllib`` 的同步 HTTP（经 ``asyncio.to_thread`` 进事件循环）。

约束（任务 2 验收"路径/远端图片处理"条）：

- 只发 **http/https**，其余 scheme 直接拒；
- 响应体**流式读取**、超过 ``MAX_RESPONSE_BYTES`` 立即断——不把无上限的字节读进内存；
- 超时交给 socket（``HttpRequest.timeout``），``api.py`` 另用 ``asyncio.wait_for`` 兜底；
- 密钥已在 ``HttpRequest.headers`` 里，本层**不记日志**——没有任何字节落到磁盘或控制台。

只依赖标准库；不 import astrbot。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

from .providers import HttpRequest, HttpResponse, ProviderError, _validated_base_url

MAX_RESPONSE_BYTES = 20 * 1024 * 1024
"""响应体上限：与缓存单张上限同量级，再大就是异常。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝所有跳转，避免 urllib 将 Authorization 转发到另一个来源。"""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def urllib_transport(req: HttpRequest) -> HttpResponse:
    """同步执行一次 HTTP POST。所有失败统一翻成 ``ProviderError``（文案安全、无密钥）。"""
    _validated_base_url(req.url, "生图服务")
    request = urllib.request.Request(
        req.url,
        data=bytes(req.body),
        headers=dict(req.headers),
        method="POST",
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=req.timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            headers = getattr(response, "headers", None)
            content_type = str(headers.get("Content-Type", "")) if headers else ""
            body = _read_capped(response)
    except urllib.error.HTTPError as error:
        try:
            # Error bodies are untrusted and not useful for protocol decisions. Close
            # the response immediately instead of retaining or echoing their contents.
            return HttpResponse(status=int(error.code or 500))
        finally:
            error.close()
    except urllib.error.URLError:
        raise ProviderError("连不上生图服务；请检查服务地址和网络") from None
    except TimeoutError:
        raise TimeoutError("连接生图服务超时") from None
    except OSError:
        raise ProviderError("生图服务连接失败") from None
    return HttpResponse(status=status, body=body, content_type=content_type)


def _read_capped(source: Any) -> bytes:
    """流式读取、超限即断（``HTTPError`` 也是 file-like，能读出错误 body 供摘要）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ProviderError("生图服务的响应超过 20 MB 上限，已中断")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["MAX_RESPONSE_BYTES", "urllib_transport"]
