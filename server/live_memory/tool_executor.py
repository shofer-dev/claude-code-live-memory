"""Read-only tool executor for the Live Memory.

Only standalone-feasible tools are implemented (ripgrep / glob / git / file
read); host-coupled tools that need an IDE (semantic search, LSP, diagnostics)
are intentionally omitted. Schema and executor parameter names are kept unified.

Every tool is read-only and **path-jailed to the workspace cwd** (no traversal).
Output is truncated to MAX_TOOL_OUTPUT_BYTES.
"""
from __future__ import annotations
from typing import Any

import asyncio
import fnmatch
import glob as globmod
import json
import os
import shutil
from pathlib import Path

from .models import ToolResult

MAX_TOOL_OUTPUT_BYTES = 200_000

# Tools that pull a *file's content* into the model's context (full or a fragment).
# A change to such a file can stale the model's knowledge, so the manager records
# each as a "read" (→ the file-change notification set). Search/listing tools
# (Grep, Glob, find_paths, git_search, get_changed_files) are intentionally NOT
# here — they expose names/snippets/commits, not enough content to track for
# staleness. Add any future content-reading tool (e.g. a multi-file read) here.
FILE_READING_TOOLS: set[str] = {"Read"}
_RG = shutil.which("rg")
_GIT = shutil.which("git")


def _truncate(text: str) -> str:
    b = text.encode("utf-8", "replace")
    if len(b) <= MAX_TOOL_OUTPUT_BYTES:
        return text
    return b[:MAX_TOOL_OUTPUT_BYTES].decode("utf-8", "ignore") + (
        f"\n[truncated: {len(b)} bytes total, showing first {MAX_TOOL_OUTPUT_BYTES}]"
    )


# ── Anthropic tool schemas (input_schema), unified with the handlers ──
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "Read",
        "description": "Read a slice of a UTF-8 text file, returned with line numbers. The path is relative to the workspace root.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path relative to the workspace root."},
                "offset": {"type": "integer", "description": "1-based start line (default 1)."},
                "limit": {"type": "integer", "description": "Max lines to read (default 2000)."},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "Grep",
        "description": "Search file contents with ripgrep (regex). By default returns matching 'file:line:text'; set output_mode to 'files_with_matches' for paths only, or 'count' for per-file match counts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {"type": "string", "description": "File or directory to search in, relative to the workspace (default: whole workspace)."},
                "glob": {"type": "string", "description": "Glob to restrict files, e.g. '*.py' or '**/*.ts'."},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"], "description": "content (default): matching lines; files_with_matches: matching file paths; count: match count per file."},
                "-i": {"type": "boolean", "description": "Case-insensitive search (default true)."},
                "head_limit": {"type": "integer", "description": "Cap on results returned. Default 100, max 1000."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "Glob",
        "description": "Find files by glob pattern (relative to the workspace root). Returns matching paths, sorted. Use find_paths instead when you need directories, a type/depth filter, or to enumerate the tree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob, e.g. '**/*.ts' (default '**/*')."},
                "path": {"type": "string", "description": "Directory to search in, relative to the workspace (default: root)."},
                "limit": {"type": "integer", "description": "Max results (default 100)."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "find_paths",
        "description": "Walk the directory tree and list entries (files and/or directories) under a path, filtered like the `find` CLI — by name pattern, entry type, and depth. Use this to explore structure or list directories; use Glob when you just want files matching a glob.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to walk, relative to the workspace (default: root)."},
                "name": {"type": "string", "description": "Glob on the entry's base name, e.g. '*.py' or '*config*' (default: all)."},
                "type": {"type": "string", "enum": ["file", "dir", "any"], "description": "Restrict to files, directories, or both (default: any)."},
                "max_depth": {"type": "integer", "description": "Max directory depth to descend (1 = immediate children). Default: unlimited."},
                "limit": {"type": "integer", "description": "Max entries returned (default 200, max 2000)."},
            },
            "required": [],
        },
    },
    {
        "name": "get_changed_files",
        "description": "Files changed in the working tree (git status --porcelain).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "git_search",
        "description": "Inspect git commit history. Filter by commit-message text (query) and/or a file/dir path, and optionally include the patch (diff) for each commit. With no query and no path, returns the most recent commits.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Case-insensitive substring to match in commit messages (git log --grep)."},
                "path": {"type": "string", "description": "Limit history to commits touching this file or directory (relative to the workspace)."},
                "show_diff": {"type": "boolean", "description": "Include each commit's patch (git log -p). Verbose — keep max_results small. Default false."},
                "max_results": {"type": "integer", "description": "Default 20 (10 when show_diff), max 50."},
            },
            "required": [],
        },
    },
]


class ToolExecutor:
    def __init__(self, cwd: str):
        self.cwd = Path(cwd).resolve()

    # ── path jail ──
    def _resolve(self, rel: str) -> Path:
        p = (self.cwd / rel).resolve()
        if p != self.cwd and self.cwd not in p.parents:
            raise ValueError(f"Path '{rel}' is outside the workspace.")
        return p

    async def execute(self, name: str, args_json: str) -> ToolResult:
        try:
            args = json.loads(args_json) if args_json and args_json.strip() else {}
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError as e:
            return ToolResult("", content=f"Invalid JSON arguments for {name}: {e}", is_error=True)
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolResult("", content=f"Tool '{name}' is not available to the live memory.", is_error=True)
        try:
            res: ToolResult = await handler(args)
            res.content = _truncate(res.content)
            return res
        except Exception as e:  # noqa: BLE001 — surface tool errors to the model
            return ToolResult("", content=f"Error executing {name}: {e}", is_error=True)

    async def _run(self, argv: list[str]) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(self.cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    # ── handlers (named _t_<tool>; first positional ToolResult arg is ignored id) ──
    async def _t_Read(self, a: dict[str, Any]) -> ToolResult:
        path = a.get("file_path") or a.get("path")
        if not path:
            return ToolResult("", "Missing required parameter 'file_path'.", True)
        offset = max(1, int(a.get("offset", 1)))
        limit = max(1, int(a.get("limit", 2000)))
        full = self._resolve(path)
        text = full.read_text(encoding="utf-8", errors="replace")
        lines = text.split("\n")
        total = len(lines)
        chunk = lines[offset - 1: offset - 1 + limit]
        numbered = "\n".join(f"{offset + i:>6}\t{ln}" for i, ln in enumerate(chunk))
        body = f"{path} (lines {offset}-{offset - 1 + len(chunk)} of {total}):\n{numbered}"
        if offset - 1 + limit < total:
            body += f"\n[truncated; file has {total} lines total]"
        return ToolResult("", body)

    async def _t_Grep(self, a: dict[str, Any]) -> ToolResult:
        pattern = a.get("pattern") or a.get("regex") or a.get("query")
        if not pattern:
            return ToolResult("", "Missing required parameter 'pattern'.", True)
        if not _RG:
            return ToolResult("", "ripgrep (rg) is not installed on the server.", True)
        limit = min(int(a.get("head_limit", a.get("max_results", 100))), 1000)
        argv = [_RG, "--no-heading", "--color", "never", "-m", str(limit)]
        mode = a.get("output_mode", "content")
        if mode == "files_with_matches":
            argv.append("-l")
        elif mode == "count":
            argv.append("-c")
        else:  # content
            argv.append("--line-number")
        if a.get("-i", a.get("case_sensitive", True)):  # default: case-insensitive
            argv.append("-i")
        glob = a.get("glob") or a.get("file_pattern")
        if glob:
            argv += ["-g", glob]
        argv += ["-e", pattern]
        sub = a.get("path")
        if sub:
            argv.append(str(self._resolve(sub)))
        code, out, err = await self._run(argv)
        if code not in (0, 1):
            return ToolResult("", f"Grep failed: {err.strip() or out.strip()}", True)
        return ToolResult("", out.strip() or "No matches found.")

    async def _t_find_paths(self, a: dict[str, Any]) -> ToolResult:
        base = self._resolve(a["path"]) if a.get("path") else self.cwd
        name = a.get("name")
        etype = a.get("type", "any")
        md = a.get("max_depth")
        max_depth = int(md) if md is not None else None
        limit = min(max(1, int(a.get("limit", 200))), 2000)
        skip = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv"}
        base_depth = len(base.parts)
        rels: list[str] = []

        def matches(basename: str) -> bool:
            return name is None or fnmatch.fnmatch(basename, str(name))

        for root, dirs, files in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in skip)
            depth = len(Path(root).parts) - base_depth + 1  # children of base are depth 1
            if etype in ("dir", "any"):
                for d in dirs:
                    if matches(d) and len(rels) < limit:
                        rels.append(os.path.relpath(os.path.join(root, d), self.cwd) + "/")
            if etype in ("file", "any"):
                for fn in sorted(files):
                    if matches(fn) and len(rels) < limit:
                        rels.append(os.path.relpath(os.path.join(root, fn), self.cwd))
            if max_depth is not None and depth >= max_depth:
                dirs[:] = []  # stop descending past the requested depth
            if len(rels) >= limit:
                break
        body = "\n".join(rels)
        if len(rels) >= limit:
            body += f"\n[truncated at {limit} entries]"
        return ToolResult("", body or "No entries matched.")

    async def _t_Glob(self, a: dict[str, Any]) -> ToolResult:
        pattern = a.get("pattern") or "**/*"
        limit = max(1, int(a.get("limit", 100)))
        base = self._resolve(a["path"]) if a.get("path") else self.cwd
        matches = []
        for p in globmod.iglob(str(base / pattern), recursive=True):
            if any(part in {"node_modules", ".git", "__pycache__", "dist", "build", ".venv"} for part in Path(p).parts):
                continue
            matches.append(os.path.relpath(p, self.cwd))
            if len(matches) >= limit:
                break
        body = "\n".join(sorted(matches))
        if len(matches) >= limit:
            body += f"\n[truncated at {limit} entries]"
        return ToolResult("", body or "No files matched.")

    async def _t_get_changed_files(self, a: dict[str, Any]) -> ToolResult:
        if not _GIT:
            return ToolResult("", "git is not installed on the server.", True)
        code, out, err = await self._run([_GIT, "status", "--porcelain"])
        if code != 0:
            return ToolResult("", f"git status failed: {err.strip()}", True)
        return ToolResult("", out.strip() or "(no changes)")

    async def _t_git_search(self, a: dict[str, Any]) -> ToolResult:
        if not _GIT:
            return ToolResult("", "git is not installed on the server.", True)
        query = a.get("query")
        path = a.get("path")
        show_diff = bool(a.get("show_diff", False))
        n = min(int(a.get("max_results", 10 if show_diff else 20)), 50)
        argv = [_GIT, "log", f"-n{n}", "-i", "--date=short"]
        argv.append("-p" if show_diff else "--pretty=format:%h | %ad | %an | %s")
        if query:
            argv += ["--grep", str(query)]
        if path:
            argv += ["--", str(self._resolve(str(path)))]
        code, out, err = await self._run(argv)
        if code != 0:
            return ToolResult("", f"git log failed: {err.strip()}", True)
        return ToolResult("", out.strip() or "No matching commits.")
