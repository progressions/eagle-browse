"""Collapsed library presentation keeps filtering, ordering and paging intact."""
import unittest
from types import SimpleNamespace

from sets import collapse_sets


def asset(i, *tags):
    return SimpleNamespace(id=str(i), tags=list(tags))


class CollapsedSetsTest(unittest.TestCase):
    def test_five_assets_become_three_thumbnails(self):
        items = [asset(1, "set:a"), asset(2), asset(3, "set:a"),
                 asset(4, "set:a"), asset(5)]
        self.assertEqual(collapse_sets(items), [items[0], items[1], items[4]])
        self.assertEqual(len(items), 5)

    def test_representative_follows_sort_and_filtered_members(self):
        items = [asset(1, "set:a"), asset(2, "set:a"), asset(3, "set:b")]
        self.assertEqual(collapse_sets(reversed(items)), [items[2], items[1]])
        self.assertEqual(collapse_sets(items[1:]), [items[1], items[2]])

    def test_collapse_before_paging_removes_distant_members(self):
        first, last = asset(1, "set:a"), asset(999, "set:a")
        items = [first] + [asset(i) for i in range(2, 302)] + [last]
        collapsed = collapse_sets(items)
        self.assertEqual(len(collapsed), 301)
        self.assertNotIn(last, collapsed[200:])

    def test_primary_set_matches_existing_badge_semantics(self):
        items = [asset(1, "set:a", "set:b"), asset(2, "set:b"),
                 asset(3, "set:a"), asset(4, "ordinary-tag")]
        self.assertEqual(collapse_sets(items), [items[0], items[1], items[3]])

    def test_empty_and_ungrouped(self):
        self.assertEqual(collapse_sets([]), [])
        items = [asset(1), asset(2, "tag")]
        self.assertEqual(collapse_sets(items), items)
