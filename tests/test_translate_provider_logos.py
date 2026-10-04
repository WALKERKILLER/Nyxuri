from __future__ import annotations

from tests.translate_support import ENTRY, setUpModule, tearDownModule  # noqa: F401

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from orbit_translate.config import ConfigError, parse_config
from orbit_translate.logos import (
    LOGO_ROOT, LOGO_TTL_SECONDS, MAX_CACHE_BYTES, MAX_SVG_BYTES,
    ProviderLogoStore, _NoRedirect, _download, colored_svg, sanitize_svg,
)
from orbit_translate.settings import SettingsDocument
from orbit_translate.theme import provider_icon_path


DEFAULT = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M0 0L24 24" stroke="currentColor"/></svg>'
LOGO = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" fill="currentColor"/></svg>'


class ProviderLogoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = ProviderLogoStore(self.directory / "logos")

    def test_mymemory_has_packaged_official_png(self) -> None:
        path = provider_icon_path("mymemory")
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "mymemory.png")
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(int.from_bytes(data[16:20], "big"), 32)
        self.assertEqual(int.from_bytes(data[20:24], "big"), 32)

    def test_provider_identity_survives_settings_save_and_reload(self) -> None:
        config = self.directory / "settings.toml"
        document = SettingsDocument(config)
        document.put_provider(None, {"id": "fixture", "name": "Custom label", "type": "ollama", "enabled": False, "model": "fixture", "catalog_provider": "openai"})
        document.put_provider(0, {"name": "Edited label", "model": "other-fixture"})
        document.save()
        reloaded = SettingsDocument(config)
        self.assertEqual(reloaded.config.providers[0].catalog_provider, "openai")
        self.assertEqual(reloaded.providers[0]["name"], "Edited label")

    def test_unsafe_provider_ids_are_rejected_before_transport(self) -> None:
        for provider in ("../openai", "https://example.com", "openai?key=fixture", "/openai", "openai\n"):
            with self.subTest(provider=provider), patch("orbit_translate.logos._download") as download:
                self.assertIsNone(self.store.load(provider))
                download.assert_not_called()
                with patch("orbit_translate.logos.urllib.request.build_opener") as opener:
                    self.assertIsNone(_download(provider))
                    opener.assert_not_called()
                with self.assertRaises(ConfigError):
                    parse_config({"providers": [{"id": "fixture", "type": "ollama", "model": "fixture", "catalog_provider": provider}]})

    def test_valid_logo_is_cached_and_fresh_load_has_no_network(self) -> None:
        with patch("orbit_translate.logos._download", side_effect=lambda provider: sanitize_svg(DEFAULT if provider == "default" else LOGO)) as download:
            self.assertEqual(self.store.load("openai"), sanitize_svg(LOGO))
            self.assertEqual(download.call_count, 2)
        with patch("orbit_translate.logos._download") as download:
            self.assertEqual(self.store.cached("openai"), sanitize_svg(LOGO))
            self.assertEqual(self.store.load("openai"), sanitize_svg(LOGO))
            download.assert_not_called()
        path = self.store.cache_dir / "openai.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("Permission is hereby granted", json.loads(path.read_bytes())["license_notice"])

    def test_default_logo_becomes_a_cached_letter_fallback(self) -> None:
        with patch("orbit_translate.logos._download", return_value=sanitize_svg(DEFAULT)):
            self.assertIsNone(self.store.load("missing-provider"))
        with patch("orbit_translate.logos._download") as download:
            self.assertIsNone(self.store.cached("missing-provider"))
            self.assertIsNone(self.store.load("missing-provider"))
            download.assert_not_called()

    def test_missing_default_or_failed_transport_keeps_fallback(self) -> None:
        with patch("orbit_translate.logos._download", return_value=None):
            self.assertIsNone(self.store.load("openai"))
        self.assertFalse((self.store.cache_dir / "openai.json").exists())

    def test_stale_logo_is_retained_offline(self) -> None:
        with patch("orbit_translate.logos._download", side_effect=lambda provider: sanitize_svg(DEFAULT if provider == "default" else LOGO)):
            self.store.load("openai")
        with patch("orbit_translate.logos.time.time", return_value=time.time() + LOGO_TTL_SECONDS + 1), patch("orbit_translate.logos._download", return_value=None):
            self.assertEqual(self.store.load("openai"), sanitize_svg(LOGO))

    def test_bad_cache_and_oversized_cache_are_ignored(self) -> None:
        self.store.cache_dir.mkdir()
        path = self.store.cache_dir / "openai.json"
        for content in (b"{invalid", b"x" * (MAX_CACHE_BYTES + 1)):
            path.write_bytes(content)
            self.assertIsNone(self.store.cached("openai"))

    def test_cache_write_failure_does_not_hide_valid_fetched_logo(self) -> None:
        with patch("orbit_translate.logos._download", side_effect=lambda provider: sanitize_svg(DEFAULT if provider == "default" else LOGO)), patch("orbit_translate.logos.os.replace", side_effect=OSError):
            self.assertEqual(self.store.load("openai"), sanitize_svg(LOGO))
        self.assertEqual(list(self.store.cache_dir.glob(".logo-*")), [])

    def test_svg_sanitizer_refuses_active_external_and_unbounded_content(self) -> None:
        for data in (
            b'<svg><script>fixture</script></svg>',
            b'<svg><image href="https://example.com/a"/></svg>',
            b'<svg><use href="file:///tmp/fixture"/></svg>',
            b'<svg><path fill="url(https://example.com/a)"/></svg>',
            b'<svg><path onload="fixture"/></svg>',
            b'<!DOCTYPE svg [<!ENTITY a "fixture">]><svg/>',
            b"x" * (MAX_SVG_BYTES + 1),
            b'<svg>' + b'<g>' * 40 + b'</g>' * 40 + b'</svg>',
        ):
            with self.subTest(data=data[:80]):
                self.assertIsNone(sanitize_svg(data))

    def test_svg_uses_palette_color_without_mutating_cached_data(self) -> None:
        rendered = colored_svg(LOGO, "#aabbcc")
        self.assertIsNotNone(rendered)
        self.assertIn(b"#aabbcc", rendered)
        self.assertNotIn(b"currentColor", rendered)
        self.assertTrue(rendered.startswith(b"<svg "))
        self.assertIn(b"currentColor", LOGO)
        self.assertIsNone(colored_svg(LOGO, "bad-color"))

    def test_transport_is_public_bounded_and_rejects_redirects(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = LOGO_ROOT + "openai.svg"
        response.read.return_value = LOGO
        opener = MagicMock()
        opener.open.return_value = response
        with patch("orbit_translate.logos.urllib.request.build_opener", return_value=opener):
            self.assertEqual(_download("openai"), sanitize_svg(LOGO))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, LOGO_ROOT + "openai.svg")
            self.assertFalse(request.has_header("Authorization"))
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
            response.read.assert_called_once_with(MAX_SVG_BYTES + 1)
            response.geturl.return_value = "https://example.com/redirect.svg"
            self.assertIsNone(_download("openai"))
        with self.assertRaises(ValueError):
            _NoRedirect().redirect_request(None, None, None, None, None, "https://example.com")


if __name__ == "__main__":
    unittest.main()
