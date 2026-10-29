"""离线覆盖生产 urllib transport 的安全边界。"""

from __future__ import annotations

import io
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.imagery.providers import HttpRequest, ProviderError  # noqa: E402
from core.imagery.transport import MAX_RESPONSE_BYTES, _NoRedirectHandler, _read_capped, urllib_transport  # noqa: E402


class _OversizeStream:
    def __init__(self) -> None:
        self.remaining = MAX_RESPONSE_BYTES + 1

    def read(self, size: int) -> bytes:
        count = min(size, self.remaining)
        self.remaining -= count
        return b"x" * count


class _RedirectingOpener:
    """模拟 302：仅调用已注册 handler，不接触网络。"""

    def __init__(self, handlers: tuple[object, ...]) -> None:
        self.handler = next(
            handler for handler in handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        self.response_stream = io.BytesIO(b"remote body")
        self.request_count = 0

    def open(self, request: object, timeout: float) -> object:
        self.request_count += 1
        redirected = self.handler.redirect_request(
            request, self.response_stream, 302, "Found", {}, "https://other.example/collect"
        )
        if redirected is not None:
            raise AssertionError("redirect handler unexpectedly created another request")
        raise urllib.error.HTTPError(
            getattr(request, "full_url"), 302, "Found", {}, self.response_stream
        )


class _FailingOpener:
    def open(self, request: object, timeout: float) -> object:
        raise urllib.error.URLError("https://other.example/?Authorization=sk-sensitive")


class ImageryTransportTest(unittest.TestCase):
    def test_redirect_is_rejected_and_error_response_is_closed(self) -> None:
        opener = _RedirectingOpener((_NoRedirectHandler(),))
        with patch("core.imagery.transport.urllib.request.build_opener", return_value=opener):
            response = urllib_transport(
                HttpRequest(
                    "http://127.0.0.1:7860/sdapi/v1/txt2img",
                    headers={"Authorization": "Bearer sk-sensitive"},
                )
            )
        self.assertEqual(response.status, 302)
        self.assertEqual(response.body, b"")
        self.assertEqual(opener.request_count, 1)
        self.assertTrue(opener.response_stream.closed)

    def test_url_error_does_not_expose_reason_or_url(self) -> None:
        with patch("core.imagery.transport.urllib.request.build_opener", return_value=_FailingOpener()):
            with self.assertRaises(ProviderError) as ctx:
                urllib_transport(HttpRequest("https://api.example/v1/images/generations"))
        self.assertNotIn("other.example", str(ctx.exception))
        self.assertNotIn("sk-sensitive", str(ctx.exception))

    def test_response_stream_is_capped(self) -> None:
        with self.assertRaises(ProviderError) as ctx:
            _read_capped(_OversizeStream())
        self.assertIn("20 MB", str(ctx.exception))

    def test_invalid_request_url_is_rejected_before_opener(self) -> None:
        opener = _FailingOpener()
        with patch("core.imagery.transport.urllib.request.build_opener", return_value=opener) as build:
            with self.assertRaises(ProviderError):
                urllib_transport(HttpRequest("https://user:pass@api.example/path"))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
