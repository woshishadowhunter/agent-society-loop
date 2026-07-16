"""Bounded tools for inspecting and changing a local repository checkout."""

from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Iterator

from .domain import ToolRisk, VerificationResult, WorkspaceSnapshot
from .storage import SQLiteRepository
from .tools import ToolContext


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
        raw = path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"workspace file is not UTF-8 text: {value}") from None
        return {
            "path": path.relative_to(self.root).as_posix(),
            "content": content,
            "sha256": _sha256_bytes(raw),
        }


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


class WorkspaceWriteFileTool:
    name = "workspace_write_file"
    description = "Atomically replace one UTF-8 file using its expected SHA-256 identity"
    risk = ToolRisk.WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "expected_sha256": {"type": "string"},
        },
        "required": ["path", "content", "expected_sha256"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        repository: SQLiteRepository,
        *,
        max_bytes: int = 500_000,
        protected_paths: tuple[str, ...] = (),
    ):
        self.root = _root(root)
        self.repository = repository
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.protected_paths = frozenset(
            _normalize_relative_path(value) for value in protected_paths
        )

    def invoke_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        relative = _required_text(arguments, "path")
        content = arguments.get("content")
        expected = _required_text(arguments, "expected_sha256")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError(f"workspace content is too large: {relative}")
        path = _resolve_writable(self.root, relative, self.protected_paths)
        before_exists, before_content, current_sha = _file_state(path)
        if current_sha != expected:
            raise ValueError(
                f"stale workspace file identity for {relative}: expected {expected}, current {current_sha}"
            )
        new_sha = _sha256_bytes(encoded)
        normalized = path.relative_to(self.root).as_posix()
        snapshot = self.repository.get_workspace_snapshot(context.goal_id, normalized)
        if snapshot is None:
            snapshot = WorkspaceSnapshot.create(
                context.goal_id,
                normalized,
                before_exists,
                before_content,
                current_sha,
                new_sha,
            )
        else:
            if snapshot.restored:
                raise ValueError(f"workspace path was already restored: {normalized}")
            if snapshot.latest_sha256 != current_sha:
                raise ValueError(f"workspace file changed outside this goal: {normalized}")
            snapshot = snapshot.advance(new_sha)
        self.repository.save_workspace_snapshot(snapshot)
        try:
            _atomic_write(path, encoded)
        except OSError:
            self.repository.save_workspace_snapshot(snapshot.advance(current_sha))
            raise
        return {
            "path": normalized,
            "sha256": new_sha,
            "bytes": len(encoded),
            "created": not before_exists,
        }


class WorkspaceDiffTool:
    name = "workspace_diff"
    description = "Show the goal-owned workspace diff and its deterministic digest"
    risk = ToolRisk.READ
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        repository: SQLiteRepository,
        *,
        max_bytes: int = 300_000,
    ):
        self.root = _root(root)
        self.repository = repository
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def invoke_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        snapshots = self.repository.list_workspace_snapshots(context.goal_id)
        chunks = []
        changed = []
        for snapshot in snapshots:
            if snapshot.restored:
                continue
            path = _resolve_writable(self.root, snapshot.path)
            exists, content, current_sha = _file_state(path)
            if current_sha == snapshot.original_sha256:
                continue
            changed.append(snapshot.path)
            before_name = f"a/{snapshot.path}" if snapshot.original_exists else "/dev/null"
            after_name = f"b/{snapshot.path}" if exists else "/dev/null"
            chunks.extend(
                difflib.unified_diff(
                    snapshot.original_content.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=before_name,
                    tofile=after_name,
                )
            )
        diff = "".join(chunks)
        if len(diff.encode("utf-8")) > self.max_bytes:
            diff = _bound_text(diff, self.max_bytes)
        return {
            "changed_files": changed,
            "diff": diff,
            "workspace_digest": _workspace_digest(self.root, snapshots),
        }


class WorkspaceRestoreChangesTool:
    name = "workspace_restore_changes"
    description = "Restore all files changed by this goal to their durable original state"
    risk = ToolRisk.WRITE
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, root: str | Path, repository: SQLiteRepository):
        self.root = _root(root)
        self.repository = repository

    def invoke_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        snapshots = [
            item
            for item in self.repository.list_workspace_snapshots(context.goal_id)
            if not item.restored
        ]
        states = []
        for snapshot in snapshots:
            path = _resolve_writable(self.root, snapshot.path)
            state = _file_state(path)
            if state[2] not in {snapshot.latest_sha256, snapshot.previous_sha256}:
                raise ValueError(
                    f"workspace file changed outside this goal: {snapshot.path}"
                )
            states.append((snapshot, path))
        restored = []
        for snapshot, path in states:
            if snapshot.original_exists:
                _atomic_write(path, snapshot.original_content.encode("utf-8"))
            elif path.exists():
                path.unlink()
            self.repository.save_workspace_snapshot(
                snapshot.advance(snapshot.original_sha256, restored=True)
            )
            restored.append(snapshot.path)
        return {"restored": restored}


class WorkspaceRunCheckTool:
    name = "workspace_run_check"
    description = "Run one operator-configured verification check by name"
    risk = ToolRisk.EXECUTE
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        root: str | Path,
        repository: SQLiteRepository,
        checks: dict[str, tuple[str, ...]],
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 100_000,
    ):
        self.root = _root(root)
        self.repository = repository
        if timeout_seconds <= 0 or max_output_bytes < 1:
            raise ValueError("check timeout and output limit must be positive")
        normalized = {}
        for name, command in checks.items():
            if not name.strip() or not command or any(not str(part) for part in command):
                raise ValueError("check names and commands must not be empty")
            normalized[name.strip()] = tuple(str(part) for part in command)
        if not normalized:
            raise ValueError("at least one verification check is required")
        self.checks = normalized
        self.input_schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": sorted(self.checks)}
            },
            "required": ["name"],
            "additionalProperties": False,
        }
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes

    def approval_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        name = _required_text(arguments, "name")
        return {"name": name, "command": list(self.checks[name])}

    def invoke_with_context(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        name = _required_text(arguments, "name")
        command = self.checks.get(name)
        if command is None:
            raise ValueError(f"unknown check: {name}")
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=_minimal_environment(),
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as error:
            exit_code = -1
            stdout = _timeout_text(error.stdout)
            stderr = _timeout_text(error.stderr) + "\nverification timed out"
        duration_ms = (time.perf_counter() - started) * 1000.0
        snapshots = self.repository.list_workspace_snapshots(context.goal_id)
        digest = _workspace_digest(self.root, snapshots)
        result = VerificationResult.create(
            context.goal_id,
            context.task_id,
            name,
            command,
            exit_code == 0,
            exit_code,
            duration_ms,
            _bound_text(stdout, self.max_output_bytes),
            _bound_text(stderr, self.max_output_bytes),
            digest,
        )
        self.repository.save_verification_result(result)
        return {
            "name": result.check_name,
            "passed": result.passed,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "workspace_digest": result.workspace_digest,
        }


def _root(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir():
        raise ValueError(f"workspace directory not found: {value}")
    return root


def _resolve(root: Path, value: str) -> Path:
    supplied = Path(value)
    if supplied.is_absolute():
        raise ValueError(f"path is outside workspace: {value}")
    _reject_protected_parts(supplied, value)
    unresolved = root / supplied
    _reject_links(root, unresolved, value)
    path = unresolved.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path is outside workspace: {value}")
    return path


def _resolve_writable(
    root: Path, value: str, protected_paths: frozenset[str] = frozenset()
) -> Path:
    supplied = Path(value)
    if supplied.is_absolute():
        raise ValueError(f"path is outside workspace: {value}")
    _reject_protected_parts(supplied, value)
    normalized = _normalize_relative_path(value)
    if normalized in protected_paths:
        raise ValueError(f"workspace path is protected: {value}")
    unresolved = root / supplied
    parent = unresolved.parent.resolve()
    if not parent.is_relative_to(root):
        raise ValueError(f"path is outside workspace: {value}")
    path = unresolved.resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path is outside workspace: {value}")
    _reject_links(root, unresolved, value)
    if path.exists() and not path.is_file():
        raise ValueError(f"workspace path is not a file: {value}")
    return path


def _reject_protected_parts(path: Path, value: str) -> None:
    ignored = {item.casefold() for item in _IGNORED_DIRECTORIES}
    if any(part.casefold() in ignored for part in path.parts):
        raise ValueError(f"workspace path is protected: {value}")


def _reject_links(root: Path, unresolved: Path, value: str) -> None:
    for candidate in (unresolved, *unresolved.parents):
        if candidate == root.parent:
            break
        if candidate.is_symlink() or (
            hasattr(candidate, "is_junction") and candidate.is_junction()
        ):
            raise ValueError(f"workspace path is a link: {value}")
        if candidate == root:
            break


def _normalize_relative_path(value: str) -> str:
    supplied = Path(value)
    if supplied.is_absolute() or ".." in supplied.parts:
        raise ValueError(f"path is outside workspace: {value}")
    return supplied.as_posix()


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_state(path: Path) -> tuple[bool, str, str]:
    if not path.exists():
        return False, "", "missing"
    content = path.read_bytes()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"workspace file is not UTF-8 text: {path.name}") from None
    return True, text, _sha256_bytes(content)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _workspace_digest(root: Path, snapshots: list[WorkspaceSnapshot]) -> str:
    identities = []
    for snapshot in snapshots:
        path = _resolve_writable(root, snapshot.path)
        identities.append((snapshot.path, _file_state(path)[2], snapshot.restored))
    payload = "\n".join(
        f"{path}\0{identity}\0{int(restored)}"
        for path, identity, restored in sorted(identities)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bound_text(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _minimal_environment() -> dict[str, str]:
    allowed = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


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
