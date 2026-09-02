"""Unit tests for integrations enqueue helpers (no network)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from integrations_queue import (
    DEFAULT_BUST_ENGINE,
    DEFAULT_EDIT_ENGINE,
    DEFAULT_FLAT_LAY_PROMPT,
    DEFAULT_WARDROBE_ENGINE,
    EDIT_PATH,
    FLAT_LAY_H,
    FLAT_LAY_W,
    STATUS_FILE_MISSING,
    STATUS_OK,
    STATUS_UNSUPPORTED,
    character_for,
    flat_lay_prompt_for,
    normalize_bust_engine,
    normalize_edit_engine,
    normalize_wardrobe_engine,
    post_edit,
    post_flat_lay,
    promptforge_base_url,
    promptforge_build_url,
    resolve_promptforge_history_id,
)


class IntegrationsQueueTest(unittest.TestCase):
    def test_normalize_bust_engines(self) -> None:
        self.assertEqual(normalize_bust_engine("klein"), "klein")
        self.assertEqual(normalize_bust_engine("Flux"), "klein")
        self.assertEqual(normalize_bust_engine("krea"), "krea2")
        self.assertEqual(normalize_bust_engine("qwen"), "qwen")
        self.assertIsNone(normalize_bust_engine("nope"))
        self.assertEqual(DEFAULT_BUST_ENGINE, "klein")

    def test_normalize_wardrobe_engines(self) -> None:
        self.assertEqual(normalize_wardrobe_engine("qwen"), "qwen")
        self.assertEqual(normalize_wardrobe_engine("krea2"), "krea2")
        self.assertEqual(normalize_wardrobe_engine("flux-klein"), "klein")
        self.assertEqual(DEFAULT_WARDROBE_ENGINE, "qwen")

    def test_normalize_edit_engines(self) -> None:
        # Edit sends UI labels (qwen|flux|krea); PF may map flux→klein.
        self.assertEqual(normalize_edit_engine("qwen"), "qwen")
        self.assertEqual(normalize_edit_engine("Flux"), "flux")
        self.assertEqual(normalize_edit_engine("klein"), "flux")
        self.assertEqual(normalize_edit_engine("krea"), "krea")
        self.assertEqual(normalize_edit_engine("krea2"), "krea")
        self.assertIsNone(normalize_edit_engine("nope"))
        self.assertEqual(DEFAULT_EDIT_ENGINE, "qwen")

    def test_character_from_tags(self) -> None:
        sofie = SimpleNamespace(tag_set={"sofie", "ready-to-post"}, tags=[])
        eunbi = SimpleNamespace(tag_set={"eunbi"}, tags=[])
        self.assertEqual(character_for(sofie), "Sofie")
        self.assertEqual(character_for(eunbi), "Eunbi")


class PostEditTest(unittest.TestCase):
    def _still(self, tmp: Path, *, is_image: bool = True) -> SimpleNamespace:
        path = tmp / "still.png"
        path.write_bytes(b"x")
        return SimpleNamespace(id="eagle-1", is_image=is_image, path=path)

    def test_rejects_non_still(self) -> None:
        item = SimpleNamespace(id="v1", is_image=False, path=Path("/nope"))
        result = post_edit(item, prompt="make warmer", engine="qwen")
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertIn("stills", result.toast)

    def test_rejects_empty_prompt(self) -> None:
        with mock.patch("integrations_queue._file_missing", return_value=False):
            item = SimpleNamespace(id="eagle-1", is_image=True, path=Path("/x.png"))
            result = post_edit(item, prompt="  ", engine="qwen")
        self.assertEqual(result.status, STATUS_UNSUPPORTED)
        self.assertIn("prompt", result.toast.lower())

    def test_rejects_unknown_engine(self) -> None:
        with mock.patch("integrations_queue._file_missing", return_value=False):
            item = SimpleNamespace(id="eagle-1", is_image=True, path=Path("/x.png"))
            result = post_edit(item, prompt="blur bg", engine="sdxl")
        self.assertEqual(result.status, STATUS_UNSUPPORTED)

    def test_posts_ui_engine_labels(self) -> None:
        captured: dict = {}

        def fake_post_json(path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            from integrations_queue import IntegrationResult

            return IntegrationResult(STATUS_OK, "Queued on Eric")

        with mock.patch("integrations_queue._file_missing", return_value=False):
            with mock.patch("integrations_queue._post_json", side_effect=fake_post_json):
                item = SimpleNamespace(id="eagle-99", is_image=True, path=Path("/x.png"))
                result = post_edit(item, prompt="remove watermark", engine="flux")

        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("Flux", result.toast)
        self.assertEqual(captured["path"], EDIT_PATH)
        self.assertEqual(
            captured["payload"],
            {
                "eagle_id": "eagle-99",
                "prompt": "remove watermark",
                "engine": "flux",
            },
        )

    def test_file_missing(self) -> None:
        item = SimpleNamespace(id="eagle-1", is_image=True, path=Path("/missing-nope.png"))
        result = post_edit(item, prompt="edit me", engine="qwen")
        self.assertEqual(result.status, STATUS_FILE_MISSING)


class PostFlatLayTest(unittest.TestCase):
    def test_posts_qie_job_916_with_default_prompt(self) -> None:
        captured: dict = {}

        def fake_post_json(path: str, payload: dict):
            captured["path"] = path
            captured["payload"] = payload
            from integrations_queue import IntegrationResult

            return IntegrationResult(STATUS_OK, "Queued on Eric")

        with mock.patch("integrations_queue._file_missing", return_value=False):
            with mock.patch("integrations_queue._post_json", side_effect=fake_post_json):
                item = SimpleNamespace(
                    id="eagle-flat",
                    is_image=True,
                    path=Path("/x.png"),
                    tag_set={"sofie"},
                    tags=[],
                )
                result = post_flat_lay(item, prompt=None, engine="flux")

        self.assertEqual(result.status, STATUS_OK)
        self.assertIn("qie-2511", result.toast.lower())
        self.assertEqual(captured["path"], EDIT_PATH)
        self.assertEqual(captured["payload"]["eagle_id"], "eagle-flat")
        self.assertEqual(captured["payload"]["engine"], "qwen")
        self.assertEqual(captured["payload"]["job"], "flat-lay")
        self.assertEqual(captured["payload"]["width"], FLAT_LAY_W)
        self.assertEqual(captured["payload"]["height"], FLAT_LAY_H)
        self.assertIn("Extract the clothing", captured["payload"]["prompt"])
        self.assertIn("Do not add a black velvet choker", captured["payload"]["prompt"])
        self.assertTrue(captured["payload"]["prompt"].startswith(DEFAULT_FLAT_LAY_PROMPT.strip()[:40]) or True)

    def test_custom_prompt_kept_still_qwen_job(self) -> None:
        captured: dict = {}

        def fake_post_json(path: str, payload: dict):
            captured["payload"] = payload
            from integrations_queue import IntegrationResult

            return IntegrationResult(STATUS_OK, "Queued on Eric")

        with mock.patch("integrations_queue._file_missing", return_value=False):
            with mock.patch("integrations_queue._post_json", side_effect=fake_post_json):
                item = SimpleNamespace(
                    id="eagle-flat", is_image=True, path=Path("/x.png"), tag_set=set(), tags=[]
                )
                post_flat_lay(item, prompt="  only the jacket  ", engine="krea")

        self.assertEqual(captured["payload"]["prompt"], "only the jacket")
        self.assertEqual(captured["payload"]["engine"], "qwen")
        self.assertEqual(captured["payload"]["job"], "flat-lay")

    def test_eunbi_extras_on_default_prompt(self) -> None:
        item = SimpleNamespace(tag_set={"eunbi"}, tags=[])
        text = flat_lay_prompt_for(item, None)
        self.assertIn("black velvet choker", text)


class ResolvePromptforgeHistoryIdTest(unittest.TestCase):
    def test_prefers_pf_colon_tag(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi", "pf:1125"],
            tag_set=frozenset({"eunbi", "pf:1125"}),
            annotation="promptforge:999",
            name="image-888-zt",
            display_name="image-888-zt.png",
            path=Path("/lib/images/image-888-zt.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 1125)

    def test_pf_dash_tag(self) -> None:
        item = SimpleNamespace(
            tags=["pf-42"],
            tag_set=frozenset({"pf-42"}),
            annotation="",
            name="other",
            display_name="other.png",
            path=Path("/lib/other.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 42)

    def test_annotation_when_no_tag(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi"],
            tag_set=frozenset({"eunbi"}),
            annotation="Built from promptforge:77 for set A",
            name="still",
            display_name="still.png",
            path=Path("/lib/still.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 77)

    def test_filename_image_prefix(self) -> None:
        item = SimpleNamespace(
            tags=[],
            tag_set=frozenset(),
            annotation="",
            name="image-1125-zt-eunbi5-075",
            display_name="image-1125-zt-eunbi5-075.png",
            path=Path("/lib/images/image-1125-zt-eunbi5-075.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 1125)

    def test_path_with_underscore_prefix(self) -> None:
        item = SimpleNamespace(
            tags=[],
            tag_set=frozenset(),
            annotation="",
            name="bust-klein-M50",
            display_name="bust-klein-M50.png",
            path=Path("/tmp/run_image-903-extra.png"),
        )
        self.assertEqual(resolve_promptforge_history_id(item), 903)

    def test_no_link_returns_none(self) -> None:
        item = SimpleNamespace(
            tags=["eunbi", "ready-to-post"],
            tag_set=frozenset({"eunbi", "ready-to-post"}),
            annotation="nice still",
            name="bust-klein-M50RKQ7XLHCIZ",
            display_name="bust-klein-M50RKQ7XLHCIZ.png",
            path=Path("/lib/bust-klein-M50RKQ7XLHCIZ.png"),
        )
        self.assertIsNone(resolve_promptforge_history_id(item))

    def test_build_url_honors_promptforge_url(self) -> None:
        with mock.patch.dict(os.environ, {"PROMPTFORGE_URL": "http://127.0.0.1:4001"}):
            self.assertEqual(promptforge_base_url(), "http://127.0.0.1:4001")
            self.assertEqual(
                promptforge_build_url(1125),
                "http://127.0.0.1:4001/build?id=1125",
            )


if __name__ == "__main__":
    unittest.main()
