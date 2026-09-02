"""Synthetic catalog fixture + query cache warm path."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from library import EagleLibrary
from synth_catalog import bench_queries, write_synth_library


class SynthCatalogTest(unittest.TestCase):
    def test_load_and_warm_query_reuse(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eagle-synth-test-") as tmp:
            root = Path(tmp)
            write_synth_library(root, 120)
            library = EagleLibrary(root)
            library.load()
            self.assertEqual(len(library.items), 120)
            stats = bench_queries(library, repeats=3)
            self.assertEqual(int(stats["items"]), 120)
            # Warm queries should be much cheaper than a cold scan.
            self.assertLess(stats["warm_mean_s"], stats["cold_s"])
            first = library.query(include_deleted=False)
            second = library.query(include_deleted=False)
            self.assertIs(first, second)
