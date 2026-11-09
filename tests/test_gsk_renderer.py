"""GSK renderer must be chosen before GTK imports, defaulting to Vulkan."""

from __future__ import annotations

import unittest
from pathlib import Path

from gsk_compat import DEFAULT_GSK_RENDERER, configure_gsk_renderer

ROOT = Path(__file__).resolve().parents[1]


class ConfigureGskRendererTest(unittest.TestCase):
    def test_defaults_to_vulkan_when_unset(self) -> None:
        env: dict[str, str] = {}
        self.assertEqual(configure_gsk_renderer(env), "vulkan")
        self.assertEqual(env["GSK_RENDERER"], "vulkan")
        self.assertEqual(DEFAULT_GSK_RENDERER, "vulkan")

    def test_blank_and_whitespace_count_as_unset(self) -> None:
        for value in ("", "  ", "\t"):
            with self.subTest(value=repr(value)):
                env = {"GSK_RENDERER": value}
                self.assertEqual(configure_gsk_renderer(env), "vulkan")
                self.assertEqual(env["GSK_RENDERER"], "vulkan")

    def test_honors_explicit_renderer(self) -> None:
        for value in ("cairo", "gl", "ngl", "vulkan"):
            with self.subTest(value=value):
                env = {"GSK_RENDERER": value}
                self.assertEqual(configure_gsk_renderer(env), value)
                self.assertEqual(env["GSK_RENDERER"], value)

    def test_strips_explicit_renderer(self) -> None:
        env = {"GSK_RENDERER": " cairo "}
        self.assertEqual(configure_gsk_renderer(env), "cairo")
        self.assertEqual(env["GSK_RENDERER"], "cairo")


class GskRendererImportOrderTest(unittest.TestCase):
    def test_app_configures_gsk_before_importing_gi(self) -> None:
        text = (ROOT / "app.py").read_text()
        configure_at = text.index("configure_gsk_renderer()")
        gi_at = text.index("import gi")
        self.assertLess(configure_at, gi_at)

    def test_pkgbuild_depends_on_gst_plugin_gtk4(self) -> None:
        text = (ROOT / "PKGBUILD").read_text()
        self.assertIn("'gst-plugin-gtk4'", text)


if __name__ == "__main__":
    unittest.main()
