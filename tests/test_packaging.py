from __future__ import annotations

import unittest
from importlib.metadata import metadata
from pathlib import Path

import agent_society_loop


ROOT = Path(__file__).resolve().parents[1]


class PackagingContractTests(unittest.TestCase):
    def test_project_metadata_matches_runtime_and_exposes_public_links(self) -> None:
        package_metadata = metadata("agent-society-loop")
        project_urls = dict(
            entry.split(", ", 1) for entry in package_metadata.get_all("Project-URL", [])
        )

        self.assertEqual(package_metadata["Version"], agent_society_loop.__version__)
        self.assertEqual(package_metadata["License-Expression"], "MIT")
        self.assertEqual(
            project_urls["Changelog"],
            "https://github.com/woshishadowhunter/agent-society-loop/blob/main/CHANGELOG.md",
        )

    def test_release_workflow_uses_trusted_publishing(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("types: [published]", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("environment: pypi", workflow)
        self.assertIn("vars.PYPI_TRUSTED_PUBLISHING == 'true'", workflow)
        self.assertIn("pypa/gh-action-pypi-publish@release/v1", workflow)

    def test_readme_offers_release_install_and_reproducible_evidence(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("releases/download/v1.2.0/agent_society_loop-1.2.0-py3-none-any.whl", readme)
        self.assertIn("docs/benchmark.md", readme)


if __name__ == "__main__":
    unittest.main()
