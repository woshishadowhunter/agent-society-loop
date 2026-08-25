"""Tests for the eight-consciousness plugin manifest."""

import unittest

from seed_society.plugins import (
    Consciousness,
    SOCIETY_MANIFESTS,
    describe_society,
    list_plugins,
)


class PluginManifestTests(unittest.TestCase):
    def test_manifest_names_are_unique(self):
        names = [item.name for item in SOCIETY_MANIFESTS]
        self.assertEqual(len(names), len(set(names)))

    def test_manifest_modules_are_declared_plausibly(self):
        for item in SOCIETY_MANIFESTS:
            self.assertTrue(item.module.startswith("seed_society.")
                            or item.module.startswith("integrations."))
            self.assertTrue(item.provides)
            self.assertEqual(item.consciousness.value.casefold(),
                             item.consciousness.value)

    def test_dependencies_point_at_declared_plugins(self):
        known = {item.name for item in SOCIETY_MANIFESTS}
        for item in SOCIETY_MANIFESTS:
            for dependency in item.depends_on:
                self.assertIn(dependency, known, item.name)

    def test_list_plugins_filters_by_consciousness(self):
        for consciousness in Consciousness:
            entries = list_plugins(consciousness=consciousness.value)
            self.assertTrue(entries)
            self.assertTrue(
                all(entry["consciousness"] == consciousness.value for entry in entries)
            )

    def test_list_plugins_rejects_unknown_consciousness(self):
        with self.assertRaises(ValueError):
            list_plugins(consciousness="prajna")

    def test_describe_society_covers_all_consciousnesses(self):
        value = describe_society()
        group_ids = {group["id"] for group in value["consciousnesses"]}
        self.assertEqual(
            group_ids,
            {"alaya", "manas", "mano", "panca", "sila"},
        )
        listed = {
            entry["name"]
            for group in value["consciousnesses"]
            for entry in group["plugins"]
        }
        self.assertEqual(listed, {item.name for item in SOCIETY_MANIFESTS})

    def test_every_consciousness_group_is_nonempty(self):
        value = describe_society()
        for group in value["consciousnesses"]:
            self.assertTrue(group["plugins"], group["id"])


if __name__ == "__main__":
    unittest.main()
