from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from resources import data_dir


class InstalledDataTest(unittest.TestCase):
    def test_data_falls_back_to_installed_share_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected = data_dir(
                "sounds", module_dir=root / "site-packages", prefix=root
            )
            self.assertEqual(selected, root / "share/eagle-browse/sounds")

    def test_data_prefers_source_checkout_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source/phone_web"
            source.mkdir(parents=True)
            selected = data_dir("phone_web", module_dir=source.parent, prefix=root)
            self.assertEqual(selected, source)

    def test_data_directory_can_be_overridden(self) -> None:
        with patch.dict("os.environ", {"EAGLE_BROWSE_DATA_DIR": "/opt/eagle-data"}):
            self.assertEqual(data_dir("sounds"), Path("/opt/eagle-data/sounds"))


if __name__ == "__main__":
    unittest.main()
