import tempfile
import unittest
import hashlib
from pathlib import Path

from agent_society_loop.domain import ToolRisk
from agent_society_loop.storage import SQLiteRepository
from agent_society_loop.tools import ToolContext
from agent_society_loop.workspace_tools import (
    WorkspaceDiffTool,
    WorkspaceListFilesTool,
    WorkspaceReadFileTool,
    WorkspaceRestoreChangesTool,
    WorkspaceRunCheckTool,
    WorkspaceSearchTool,
    WorkspaceWriteFileTool,
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

    def test_list_files_does_not_read_symlink_targets_outside_workspace(self):
        outside = self.root.parent / f"{self.root.name}-outside-secret.txt"
        outside.write_text("outside secret", encoding="utf-8")
        link = self.root / "linked-secret.txt"
        try:
            link.symlink_to(outside)
        except OSError as error:
            outside.unlink(missing_ok=True)
            self.skipTest(f"symbolic links unavailable: {error}")
        try:
            result = WorkspaceListFilesTool(self.root).invoke({})
        finally:
            outside.unlink(missing_ok=True)

        self.assertNotIn("linked-secret.txt", result)

    def test_read_and_write_reject_symlink_paths_even_when_target_is_inside_workspace(self):
        link = self.root / "linked-app.py"
        try:
            link.symlink_to(self.root / "src" / "app.py")
        except OSError as error:
            self.skipTest(f"symbolic links unavailable: {error}")
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")

        with self.assertRaisesRegex(ValueError, "link"):
            WorkspaceReadFileTool(self.root).invoke({"path": "linked-app.py"})
        with self.assertRaisesRegex(ValueError, "link"):
            WorkspaceWriteFileTool(self.root, repository).invoke_with_context(
                {
                    "path": "linked-app.py",
                    "content": "changed\n",
                    "expected_sha256": hashlib.sha256(
                        (self.root / "src" / "app.py").read_bytes()
                    ).hexdigest(),
                },
                context,
            )
        repository.close()

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
        self.assertEqual(
            result["sha256"],
            hashlib.sha256(result["content"].encode("utf-8")).hexdigest(),
        )

    def test_search_returns_bounded_line_evidence(self):
        tool = WorkspaceSearchTool(self.root, max_results=1)

        result = tool.invoke({"query": "def parse"})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["path"], "src/app.py")
        self.assertEqual(result[0]["line"], 1)
        self.assertIn("parse", result[0]["text"])

    def test_content_addressed_write_persists_original_and_rejects_stale_hash(self):
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")
        reader = WorkspaceReadFileTool(self.root)
        writer = WorkspaceWriteFileTool(self.root, repository)
        before = reader.invoke({"path": "src/app.py"})

        result = writer.invoke_with_context(
            {
                "path": "src/app.py",
                "content": "def parse(value):\n    return value.strip()\n",
                "expected_sha256": before["sha256"],
            },
            context,
        )

        self.assertEqual(result["path"], "src/app.py")
        self.assertNotEqual(result["sha256"], before["sha256"])
        snapshot = repository.get_workspace_snapshot("goal", "src/app.py")
        self.assertEqual(snapshot.original_content, before["content"])
        self.assertEqual(snapshot.original_sha256, before["sha256"])
        with self.assertRaisesRegex(ValueError, "stale"):
            writer.invoke_with_context(
                {
                    "path": "src/app.py",
                    "content": "stale overwrite\n",
                    "expected_sha256": before["sha256"],
                },
                context,
            )
        repository.close()

    def test_new_file_uses_missing_identity_and_restore_survives_reopen(self):
        database = self.root / "state.db"
        context = ToolContext("goal", "task", "agent")
        repository = SQLiteRepository(database)
        writer = WorkspaceWriteFileTool(self.root, repository)
        writer.invoke_with_context(
            {"path": "src/new.py", "content": "VALUE = 1\n", "expected_sha256": "missing"},
            context,
        )
        repository.close()

        reopened = SQLiteRepository(database)
        restored = WorkspaceRestoreChangesTool(self.root, reopened).invoke_with_context(
            {}, context
        )

        self.assertEqual(restored["restored"], ["src/new.py"])
        self.assertFalse((self.root / "src" / "new.py").exists())
        self.assertTrue(
            reopened.get_workspace_snapshot("goal", "src/new.py").restored
        )
        reopened.close()

    def test_restore_refuses_to_overwrite_external_change(self):
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")
        before = WorkspaceReadFileTool(self.root).invoke({"path": "src/app.py"})
        WorkspaceWriteFileTool(self.root, repository).invoke_with_context(
            {
                "path": "src/app.py",
                "content": "agent change\n",
                "expected_sha256": before["sha256"],
            },
            context,
        )
        (self.root / "src" / "app.py").write_text("human change\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "changed outside"):
            WorkspaceRestoreChangesTool(self.root, repository).invoke_with_context({}, context)

        self.assertEqual(
            (self.root / "src" / "app.py").read_text(encoding="utf-8"),
            "human change\n",
        )
        repository.close()

    def test_restore_accepts_last_recorded_prewrite_identity_after_interruption(self):
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")
        before = WorkspaceReadFileTool(self.root).invoke({"path": "README.md"})
        writer = WorkspaceWriteFileTool(self.root, repository)
        first = writer.invoke_with_context(
            {
                "path": "README.md",
                "content": "first agent write\n",
                "expected_sha256": before["sha256"],
            },
            context,
        )
        snapshot = repository.get_workspace_snapshot("goal", "README.md")
        repository.save_workspace_snapshot(snapshot.advance("pending-write-hash"))

        restored = WorkspaceRestoreChangesTool(self.root, repository).invoke_with_context(
            {}, context
        )

        self.assertEqual(restored["restored"], ["README.md"])
        self.assertEqual(
            (self.root / "README.md").read_bytes(), before["content"].encode("utf-8")
        )
        self.assertNotEqual(first["sha256"], before["sha256"])
        repository.close()

    def test_diff_and_named_check_record_workspace_digest(self):
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")
        before = WorkspaceReadFileTool(self.root).invoke({"path": "README.md"})
        WorkspaceWriteFileTool(self.root, repository).invoke_with_context(
            {
                "path": "README.md",
                "content": "Parser project\nVerified\n",
                "expected_sha256": before["sha256"],
            },
            context,
        )

        diff = WorkspaceDiffTool(self.root, repository).invoke_with_context({}, context)
        check = WorkspaceRunCheckTool(
            self.root,
            repository,
            {"syntax": ("python", "-c", "print('verified')")},
        ).invoke_with_context({"name": "syntax"}, context)

        self.assertIn("+Verified", diff["diff"])
        self.assertEqual(check["workspace_digest"], diff["workspace_digest"])
        self.assertTrue(check["passed"])
        self.assertIn("verified", check["stdout"])
        self.assertEqual(
            repository.list_verification_results("goal")[0].check_name,
            "syntax",
        )
        repository.close()

    def test_named_check_rejects_unknown_name_and_bounds_output(self):
        repository = SQLiteRepository(self.root / "state.db")
        context = ToolContext("goal", "task", "agent")
        tool = WorkspaceRunCheckTool(
            self.root,
            repository,
            {"noisy": ("python", "-c", "print('x' * 1000)")},
            max_output_bytes=40,
        )

        with self.assertRaisesRegex(ValueError, "unknown check"):
            tool.invoke_with_context({"name": "other"}, context)
        result = tool.invoke_with_context({"name": "noisy"}, context)

        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 40)
        repository.close()


if __name__ == "__main__":
    unittest.main()
