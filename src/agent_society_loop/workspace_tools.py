"""Bounded read-only tools for inspecting a local repository checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from .domain import ToolRisk


_IGNORED_DIRECTORIES = {
    ".git",
    ".worktrees",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


class WorkspaceListFilesTool:
    name = "workspace_list_files"
    description = "List bounded UTF-8 text files in the local repository"
    risk = ToolRisk.READ
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, root: str | Path, *, max_files: int = 500):
        self.root = _root(root)
        if max_files < 1:
            raise ValueError("max_files must be positive")
        self.max_files = max_files

    def invoke(self, arguments: dict[str, Any]) -> list[str]:
        result = []
        for path in _candidate_files(self.root):
            if _is_binary(path):
                continue
            result.append(path.relative_to(self.root).as_posix())
            if len(result) >= self.max_files:
                break
        return sorted(result)


class WorkspaceReadFileTool:
    name = "workspace_read_file"
    description = "Read one UTF-8 text file under the local repository root"
    risk = ToolRisk.READ
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, root: str | Path, *, max_bytes: int = 200_000):
        self.root = _root(root)
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def invoke(self, arguments: dict[str, Any]) -> dict[str, str]:
        value = arguments.get("path")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("path must be a non-empty string")
        path = _resolve(self.root, value)
        if not path.is_file():
            raise ValueError(f"workspace file not found: {value}")
        if path.stat().st_size > self.max_bytes:
            raise ValueError(f"workspace file is too large: {value}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"workspace file is not UTF-8 text: {value}") from None
        return {"path": path.relative_to(self.root).as_posix(), "content": content}


class WorkspaceSearchTool:
    name = "workspace_search"
    description = "Search bounded UTF-8 repository files for a literal text query"
    risk = ToolRisk.READ
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        *,
        max_results: int = 100,
        max_files: int = 500,
        max_file_bytes: int = 200_000,
    ):
        self.root = _root(root)
        if max_results < 1:
            raise ValueError("max_results must be positive")
        self.max_results = max_results
        self.files = WorkspaceListFilesTool(self.root, max_files=max_files)
        self.reader = WorkspaceReadFileTool(self.root, max_bytes=max_file_bytes)

    def invoke(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        needle = query.casefold()
        result = []
        for relative in self.files.invoke({}):
            try:
                content = self.reader.invoke({"path": relative})["content"]
            except ValueError:
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle in line.casefold():
                    result.append(
                        {"path": relative, "line": line_number, "text": line[:500]}
                    )
                    if len(result) >= self.max_results:
                        return result
        return result


def _root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace directory not found: {value}")
    return root


def _resolve(root: Path, value: str) -> Path:
    supplied = Path(value)
    if supplied.is_absolute():
        raise ValueError(f"path is outside workspace: {value}")
    path = (root / supplied).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path is outside workspace: {value}")
    return path


def _candidate_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if any(part in _IGNORED_DIRECTORIES for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or (
            hasattr(path, "is_junction") and path.is_junction()
        ):
            continue
        if path.is_file():
            yield path


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            sample = stream.read(8192)
    except OSError:
        return True
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False
