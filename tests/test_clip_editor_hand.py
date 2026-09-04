"""Shift+E multi-path clip-editor argv (#539)."""

from __future__ import annotations

import unittest

from clip_editor_hand import clip_editor_argv, media_flag


class MediaFlagTest(unittest.TestCase):
    def test_video_and_audio(self) -> None:
        self.assertEqual(media_flag(is_video=True, is_audio=False), "--video")
        self.assertEqual(media_flag(is_video=False, is_audio=True), "--audio")
        self.assertIsNone(media_flag(is_video=False, is_audio=False))


class ClipEditorArgvTest(unittest.TestCase):
    def test_single_video(self) -> None:
        self.assertEqual(
            clip_editor_argv("/usr/bin/clip-editor", [("--video", "/a.mp4")]),
            ["/usr/bin/clip-editor", "gui", "--video", "/a.mp4"],
        )

    def test_multi_video_current_project(self) -> None:
        cmd = clip_editor_argv(
            "/usr/bin/clip-editor",
            [("--video", "/a.mp4"), ("--video", "/b.mp4")],
        )
        self.assertEqual(
            cmd,
            [
                "/usr/bin/clip-editor",
                "gui",
                "--video",
                "/a.mp4",
                "--video",
                "/b.mp4",
            ],
        )
        self.assertNotIn("--new", cmd)

    def test_new_project_all_paths(self) -> None:
        cmd = clip_editor_argv(
            "clip-editor",
            [("--video", "/a.mp4"), ("--audio", "/bed.m4a")],
            new_project=True,
        )
        self.assertEqual(
            cmd,
            [
                "clip-editor",
                "gui",
                "--new",
                "--video",
                "/a.mp4",
                "--audio",
                "/bed.m4a",
            ],
        )


if __name__ == "__main__":
    unittest.main()
