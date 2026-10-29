"""两个生图适配器的协议测试：**请求体与响应体都用真实格式**。

A1111（Stable Diffusion WebUI）与 OpenAI 兼容 Images 的请求/响应形状各自独立、
互不通用——这里逐字段钉住，防止"把不同服务当同一个协议"。

transport 桩不碰网络：协议格式体现在 ``build_request`` 的产物与 ``parse_response``
喂进的假响应里。
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _path in (_HERE, _HERE.parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from core.imagery.providers import (  # noqa: E402
    A1111Provider,
    Capabilities,
    HttpRequest,
    HttpResponse,
    ImageRequest,
    OpenAIImagesProvider,
    ProviderError,
    build_provider,
    parse_size,
)

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode("ascii")
REFERENCE = b"\x89PNG\r\n\x1a\nreference-bytes"


def a1111_response() -> HttpResponse:
    """真实形状：``{"images": ["<b64>"], "info": "..."}``。"""
    return HttpResponse(200, json.dumps({"images": [PNG_B64], "info": "{}"}).encode("utf-8"))


def openai_response(b64: str | None = PNG_B64) -> HttpResponse:
    """真实形状：``{"created": ..., "data": [{"b64_json": "..."}]}``。"""
    item = {"b64_json": b64} if b64 is not None else {"url": "https://example.com/x.png"}
    return HttpResponse(200, json.dumps({"created": 1761000000, "data": [item]}).encode("utf-8"))


class A1111RequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = A1111Provider("http://127.0.0.1:7860")

    def test_txt2img_shape(self) -> None:
        req = ImageRequest(prompt="她在浇花", negative_prompt="模糊", size="512x768", steps=25, seed=-1)
        http = self.provider.build_request(req, "")
        self.assertEqual(http.url, "http://127.0.0.1:7860/sdapi/v1/txt2img")
        body = json.loads(http.body.decode("utf-8"))
        self.assertEqual(body["prompt"], "她在浇花")
        self.assertEqual(body["negative_prompt"], "模糊")
        self.assertEqual(body["width"], 512)
        self.assertEqual(body["height"], 768)
        self.assertEqual(body["steps"], 25)
        self.assertEqual(body["seed"], -1)
        self.assertNotIn("init_images", body, "没给参考图就不该有图生图字段")

    def test_img2img_with_reference(self) -> None:
        req = ImageRequest(
            prompt="她在浇花", size="512x512", denoising=0.55,
            reference_bytes=REFERENCE, reference_mime="image/png",
        )
        http = self.provider.build_request(req, "")
        self.assertEqual(http.url, "http://127.0.0.1:7860/sdapi/v1/img2img", "带参考图走正式的 img2img 端点")
        body = json.loads(http.body.decode("utf-8"))
        self.assertEqual(body["init_images"], [base64.b64encode(REFERENCE).decode("ascii")])
        self.assertAlmostEqual(body["denoising_strength"], 0.55)

    def test_configured_model_is_sent_as_temporary_override(self) -> None:
        provider = build_provider(
            "a1111", base_url="http://127.0.0.1:7860", model="denia-checkpoint.safetensors"
        )
        body = json.loads(provider.build_request(ImageRequest(prompt="p"), "").body.decode("utf-8"))
        self.assertEqual(body["override_settings"], {"sd_model_checkpoint": "denia-checkpoint.safetensors"})
        self.assertTrue(body["override_settings_restore_afterwards"])

    def test_empty_model_leaves_a1111_service_model_unchanged(self) -> None:
        body = json.loads(self.provider.build_request(ImageRequest(prompt="p"), "").body.decode("utf-8"))
        self.assertNotIn("override_settings", body)
        self.assertNotIn("override_settings_restore_afterwards", body)

    def test_optional_auth_header(self) -> None:
        req = ImageRequest(prompt="p")
        no_key = self.provider.build_request(req, "")
        self.assertNotIn("Authorization", no_key.headers)
        with_key = self.provider.build_request(req, "sk-secret")
        self.assertEqual(with_key.headers["Authorization"], "Bearer sk-secret")

    def test_base_url_normalization(self) -> None:
        provider = A1111Provider("http://127.0.0.1:7860/")
        http = provider.build_request(ImageRequest(prompt="p"), "")
        self.assertFalse(http.url.endswith("//sdapi"))

    def test_missing_base_url_rejected(self) -> None:
        with self.assertRaises(ProviderError):
            A1111Provider("  ")

    def test_base_url_rejects_credentials_queries_fragments_and_bad_ports(self) -> None:
        for value in (
            "ftp://127.0.0.1:7860",
            "http://user:pass@127.0.0.1:7860",
            "http://127.0.0.1:7860?",
            "http://127.0.0.1:7860#",
            "http://127.0.0.1:bad",
            "http://127.0.0.1:",
            "http://127.0.0.1:70000",
            "http:///missing-host",
        ):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                A1111Provider(value)

    def test_localhost_is_a_valid_a1111_host(self) -> None:
        self.assertTrue(A1111Provider("http://localhost:7860").base_url.startswith("http://localhost"))

    def test_size_must_parse(self) -> None:
        with self.assertRaises(ProviderError):
            self.provider.build_request(ImageRequest(prompt="p", size="big"), "")


class OpenAIRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenAIImagesProvider("https://api.openai.com/v1", "gpt-image-1")

    def test_generation_shape(self) -> None:
        http = self.provider.build_request(ImageRequest(prompt="她在浇花", size="1024x1024"), "sk-test")
        self.assertEqual(http.url, "https://api.openai.com/v1/images/generations")
        self.assertEqual(http.headers["Authorization"], "Bearer sk-test")
        body = json.loads(http.body.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-image-1")
        self.assertEqual(body["prompt"], "她在浇花")
        self.assertEqual(body["n"], 1)
        self.assertEqual(body["size"], "1024x1024")
        self.assertNotIn("response_format", body, "gpt-image-* 不接受该参数（总是回 b64_json）")

    def test_dalle_gets_response_format(self) -> None:
        provider = OpenAIImagesProvider("https://api.openai.com/v1", "dall-e-3")
        body = json.loads(provider.build_request(ImageRequest(prompt="p"), "k").body.decode("utf-8"))
        self.assertEqual(body["response_format"], "b64_json")

    def test_unsupported_parameters_never_sent(self) -> None:
        """能力声明为 False 的参数：请求体里**没有字段**，不是"传了但忽略"。"""
        req = ImageRequest(
            prompt="p", negative_prompt="模糊", seed=42, steps=30,
            reference_bytes=REFERENCE, reference_mime="image/png",
        )
        body = json.loads(self.provider.build_request(req, "k").body.decode("utf-8"))
        for absent in ("seed", "steps", "negative_prompt", "init_images", "reference_image"):
            self.assertNotIn(absent, body)
        self.assertNotIn(base64.b64encode(REFERENCE).decode("ascii")[:24], http_body_text(body))

    def test_missing_key_rejected_before_request(self) -> None:
        with self.assertRaises(ProviderError):
            self.provider.build_request(ImageRequest(prompt="p"), "")

    def test_missing_model_rejected_before_request(self) -> None:
        provider = OpenAIImagesProvider("https://api.openai.com/v1", " ")
        with self.assertRaises(ProviderError) as ctx:
            provider.build_request(ImageRequest(prompt="p"), "sk-test")
        self.assertIn("imagery.model", str(ctx.exception))

    def test_url_only_response_refused(self) -> None:
        """只回 URL：如实拒绝，不下载未约束的地址。"""
        with self.assertRaises(ProviderError) as ctx:
            self.provider.parse_response(openai_response(b64=None))
        self.assertIn("b64_json", str(ctx.exception))

    def test_missing_key_env_means_error_message(self) -> None:
        provider = OpenAIImagesProvider("https://api.openai.com/v1", "gpt-image-1")
        with self.assertRaises(ProviderError) as ctx:
            provider.build_request(ImageRequest(prompt="p"), "")
        self.assertIn("api_key_env", str(ctx.exception))


def http_body_text(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


class ParseResponseTest(unittest.TestCase):
    def test_a1111_decodes_images(self) -> None:
        data, mime = A1111Provider("http://x").parse_response(a1111_response())
        self.assertEqual(data, b"\x89PNG\r\n\x1a\nfake")
        self.assertEqual(mime, "image/png")

    def test_a1111_accepts_data_url_prefix(self) -> None:
        payload = "data:image/png;base64," + PNG_B64
        resp = HttpResponse(200, json.dumps({"images": [payload]}).encode("utf-8"))
        data, _ = A1111Provider("http://x").parse_response(resp)
        self.assertEqual(data, b"\x89PNG\r\n\x1a\nfake")

    def test_a1111_non_200(self) -> None:
        resp = HttpResponse(503, json.dumps({"error": "model loading"}).encode("utf-8"))
        with self.assertRaises(ProviderError) as ctx:
            A1111Provider("http://x").parse_response(resp)
        self.assertIn("503", str(ctx.exception))

    def test_a1111_empty_images(self) -> None:
        resp = HttpResponse(200, json.dumps({"images": []}).encode("utf-8"))
        with self.assertRaises(ProviderError):
            A1111Provider("http://x").parse_response(resp)

    def test_a1111_bad_json(self) -> None:
        with self.assertRaises(ProviderError):
            A1111Provider("http://x").parse_response(HttpResponse(200, b"<html>not json</html>"))

    def test_a1111_bad_base64(self) -> None:
        resp = HttpResponse(200, json.dumps({"images": ["!!!not-base64!!!"]}).encode("utf-8"))
        with self.assertRaises(ProviderError):
            A1111Provider("http://x").parse_response(resp)

    def test_openai_decodes_b64(self) -> None:
        data, mime = OpenAIImagesProvider("https://x/v1", "m").parse_response(openai_response())
        self.assertEqual(data, b"\x89PNG\r\n\x1a\nfake")
        self.assertEqual(mime, "image/png")

    def test_openai_remote_error_body_is_never_echoed(self) -> None:
        secret = "Bearer sk-secret-value"
        resp = HttpResponse(
            400,
            json.dumps({"error": {"message": f"request failed with {secret}"}}).encode("utf-8"),
        )
        with self.assertRaises(ProviderError) as ctx:
            OpenAIImagesProvider("https://x/v1", "m").parse_response(resp)
        self.assertIn("400", str(ctx.exception))
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("request failed", str(ctx.exception))

    def test_openai_empty_data(self) -> None:
        resp = HttpResponse(200, json.dumps({"created": 1, "data": []}).encode("utf-8"))
        with self.assertRaises(ProviderError):
            OpenAIImagesProvider("https://x/v1", "m").parse_response(resp)

    def test_non_json_success_content_type_is_readable_error(self) -> None:
        resp = HttpResponse(200, b"<html>error</html>", "text/html")
        with self.assertRaises(ProviderError) as ctx:
            OpenAIImagesProvider("https://x/v1", "m").parse_response(resp)
        self.assertIn("非 JSON", str(ctx.exception))

    def test_oversized_base64_is_rejected_before_decode(self) -> None:
        from core.imagery.providers import MAX_IMAGE_BASE64_CHARS

        too_large = "A" * (MAX_IMAGE_BASE64_CHARS + 1)
        resp = HttpResponse(200, json.dumps({"data": [{"b64_json": too_large}]}).encode("utf-8"))
        with self.assertRaises(ProviderError) as ctx:
            OpenAIImagesProvider("https://x/v1", "m").parse_response(resp)
        self.assertIn("20 MB", str(ctx.exception))


class CapabilityHonestyTest(unittest.TestCase):
    def test_capability_declarations(self) -> None:
        self.assertTrue(A1111Provider.capabilities.reference_image)
        self.assertTrue(A1111Provider.capabilities.seed)
        self.assertFalse(A1111Provider.capabilities.auth_required)
        self.assertFalse(OpenAIImagesProvider.capabilities.reference_image)
        self.assertFalse(OpenAIImagesProvider.capabilities.seed)
        self.assertTrue(OpenAIImagesProvider.capabilities.auth_required)

    def test_consistency_note_says_weak_for_text_only(self) -> None:
        self.assertIn("较弱", OpenAIImagesProvider.capabilities.consistency_note)

    def test_a1111_consistency_note_does_not_promise_strong_identity(self) -> None:
        note = A1111Provider.capabilities.consistency_note
        self.assertIn("受模型和重绘幅度影响", note)
        self.assertIn("需结合实际出图测试", note)
        self.assertNotIn("较强", note)

    def test_build_provider_switches_without_code_change(self) -> None:
        """提供方切换只靠配置：同一个 build_provider，不同 id 返回不同适配器。"""
        a1111 = build_provider("a1111", base_url="http://127.0.0.1:7860", model="")
        openai = build_provider("openai", base_url="https://api.openai.com/v1", model="gpt-image-1")
        self.assertIsInstance(a1111, A1111Provider)
        self.assertIsInstance(openai, OpenAIImagesProvider)
        with self.assertRaises(ProviderError):
            build_provider("comfyui", base_url="http://x", model="")

    def test_parse_size_bounds(self) -> None:
        self.assertEqual(parse_size("512x512"), (512, 512))
        with self.assertRaises(ProviderError):
            parse_size("32x512")  # 低于 64
        with self.assertRaises(ProviderError):
            parse_size("4096x512")  # 高于 2048
        with self.assertRaises(ProviderError):
            parse_size("wide")


if __name__ == "__main__":
    unittest.main()
