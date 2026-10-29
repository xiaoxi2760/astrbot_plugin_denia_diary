"""Reject accidental credential persistence before the settings save path."""
import unittest

from core.settings import load_settings


class ImagerySettingsSafetyTest(unittest.TestCase):
    def test_environment_variable_names_are_kept(self):
        settings = load_settings({"imagery": {"api_key_env": "DENIA_IMAGE_API_KEY"}})
        self.assertEqual(settings.imagery["api_key_env"], "DENIA_IMAGE_API_KEY")
        self.assertFalse(settings.warnings)

    def test_pasted_api_key_is_not_retained_or_echoed_in_warnings(self):
        sentinel = "sk-private-test-value"
        settings = load_settings({"imagery": {"api_key_env": sentinel}})
        self.assertEqual(settings.imagery["api_key_env"], "")
        self.assertTrue(any(w.startswith("imagery.api_key_env ") for w in settings.warnings))
        self.assertNotIn(sentinel, str(settings.warnings))

    def test_credentials_and_query_strings_are_not_retained(self):
        for url in ("https://owner:private-test@example.test/v1", "https://example.test/v1?key=private-test",
                    "https://example.test/v1#private-test", "file:///private-test", "https://example.test:invalid"):
            with self.subTest(url=url):
                settings = load_settings({"imagery": {"base_url": url}})
                self.assertEqual(settings.imagery["base_url"], "")
                self.assertTrue(any(w.startswith("imagery.base_url ") for w in settings.warnings))
                self.assertNotIn("private-test", str(settings.warnings))

    def test_local_provider_address_remains_valid(self):
        settings = load_settings({"imagery": {"base_url": "http://127.0.0.1:7860"}})
        self.assertEqual(settings.imagery["base_url"], "http://127.0.0.1:7860")
        self.assertFalse(settings.warnings)
