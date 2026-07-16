import tempfile
import unittest
from pathlib import Path

from agent_society_loop.domain import ToolRisk
from agent_society_loop.workspace_tools import (
    WorkspaceListFilesTool,
    WorkspaceReadFileTool,
    WorkspaceSearchTool,
)


class WorkspaceToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text(
            "def parse(value):\n    return value\n", encoding="utf-8"
        )
        (self.root / "README.md").write_text("Parser project\n", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"\x00\x01\x02")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_list_files_is_read_only_and_skips_internal_or_binary_files(self):
        tool = WorkspaceListFilesTool(self.root)

        result = tool.invoke({})

        self.assertEqual(tool.risk, ToolRisk.READ)
        self.assertEqual(result, ["README.md", "src/app.py"])

    def test_read_file_rejects_path_escape_and_size_overflow(self):
        tool = WorkspaceReadFileTool(self.root, max_bytes=10)

        with self.assertRaisesRegex(ValueError, "outside workspace"):
            tool.invoke({"path": "../secret.txt"})
        with self.assertRaisesRegex(ValueError, "too large"):
            tool.invoke({"path": "src/app.py"})

    def test_read_file_returns_utf8_text_with_relative_identity(self):
        result = WorkspaceReadFileTool(self.root).invoke({"path": "src/app.py"})

        self.assertEqual(result["path"], "src/app.py")
        self.assertIn("def parse", result["content"])

    def test_search_returns_bounded_line_evidence(self):
        tool = WorkspaceSearchTool(self.root, max_results=1)

        result = tool.invoke({"query": "def parse"})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "src/app.py")
        self.assertEqual(result[0]["line"], 1)
        self.assertIn("parse", result[0]["text"])


if __name__ == "__main__":
    unittest.main()
