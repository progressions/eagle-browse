"""Fizzy #569: 'none are present' for tag/category rules round-trips as identity."""

import unittest

from smart_folder_rules import (
    SET_LABELS,
    SET_MODES,
    SET_NONE,
    CategoriesRule,
    OtherRule,
    TagsRule,
    decode_rule,
    encode_rule,
    rule_summary,
)


class NonePresentTests(unittest.TestCase):
    def test_none_mode_offered(self):
        assert SET_NONE in SET_MODES
        self.assertEqual(SET_LABELS[SET_NONE], "none are present")


    def test_identity_tags_decodes_as_none(self):
        rule = decode_rule({"property": "tags", "method": "identity", "value": ["x", "y"]})
        assert isinstance(rule, TagsRule)
        self.assertEqual(rule.mode, SET_NONE)
        self.assertEqual(rule.tags, ["x", "y"])


    def test_identity_folders_decodes_as_none(self):
        rule = decode_rule({"property": "folders", "method": "identity", "value": ["F1"]})
        assert isinstance(rule, CategoriesRule)
        self.assertEqual(rule.mode, SET_NONE)
        self.assertEqual(rule.folder_ids, ["F1"])


    def test_none_tags_encodes_identity(self):
        assert encode_rule(TagsRule(mode=SET_NONE, tags=["a", "b"])) == {
            "property": "tags",
            "method": "identity",
            "value": ["a", "b"],
        }


    def test_none_categories_encodes_identity(self):
        assert encode_rule(CategoriesRule(mode=SET_NONE, folder_ids=["F1"])) == {
            "property": "folders",
            "method": "identity",
            "value": ["F1"],
        }


    def test_round_trip(self):
        for raw in (
            {"property": "tags", "method": "identity", "value": ["a"]},
            {"property": "folders", "method": "identity", "value": ["F1", "F2"]},
        ):
            rule = decode_rule(raw)
            assert not isinstance(rule, OtherRule)
            self.assertEqual(encode_rule(rule), raw)


    def test_existing_modes_unchanged(self):
        self.assertEqual(encode_rule(TagsRule(mode="any", tags=["a"]))["method"], "union")
        self.assertEqual(encode_rule(TagsRule(mode="all", tags=["a"]))["method"], "subset")
        self.assertEqual(encode_rule(CategoriesRule(mode="any", folder_ids=["F"]))["method"], "intersection")


    def test_summary_mentions_none(self):
        assert "none are present" in rule_summary(TagsRule(mode=SET_NONE, tags=["a"]))


if __name__ == "__main__":
    unittest.main()
