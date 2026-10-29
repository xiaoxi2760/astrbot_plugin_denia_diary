import io
import unittest
from unittest.mock import patch

from core.imagery.validation import validate_image
from core.webui_portrait import PortraitError


class ImageryValidationTest(unittest.TestCase):
    @staticmethod
    def image_bytes(kind="PNG"):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, kind)
        return output.getvalue()

    def test_complete_static_images_are_accepted(self):
        for kind, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")):
            with self.subTest(kind=kind):
                self.assertEqual(validate_image(self.image_bytes(kind))[0], mime)

    def test_truncated_and_header_only_images_are_rejected(self):
        for data in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xfffake", self.image_bytes()[:35], self.image_bytes("JPEG")[:80], b"<html>not an image</html>"):
            with self.subTest(size=len(data)), self.assertRaises(PortraitError):
                validate_image(data)

    def test_pixel_budget_is_enforced_without_large_allocations(self):
        with patch("core.imagery.validation.MAX_PIXELS", 3), self.assertRaises(PortraitError):
            validate_image(self.image_bytes())

    def test_decoder_errors_do_not_echo_provider_bytes(self):
        with self.assertRaises(PortraitError) as error:
            validate_image(b"private-provider-sentinel")
        self.assertNotIn("private-provider-sentinel", str(error.exception))
