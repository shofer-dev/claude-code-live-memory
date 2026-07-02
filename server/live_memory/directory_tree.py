"""Workspace directory tree for the stable system-prompt prefix.

Bounded to ~10% of the context window. Skips heavy/build dirs and dotfiles
(except .gitignore). Full .gitignore semantics are approximated by SKIP_PARTS +
dotfile skipping; a pathspec-based filter can be added later.
"""
from __future__ import annotations
from typing import Any

import os
from pathlib import Path

from .constants import DEFAULT_DIRECTORY_TREE_FRACTION
from .models import estimate_tokens

SKIP_PARTS = {
    "node_modules", ".git", "__pycache__", ".cache",
    "dist", "out", "build", "target", ".next", ".turbo", ".venv", "venv",
}


def _scan(dir_path: Path, prefix: str) -> list[dict[str, Any]]:
    try:
        entries = list(os.scandir(dir_path))
    except OSError:
        return []
    # directories first, then alphabetical
    entries.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    out: list[dict[str, Any]] = []
    for e in entries:
        name = e.name
        if name.startswith("."):
            if name not in (".gitignore",):
                continue
        is_dir = e.is_dir(follow_symlinks=False)
        if is_dir and name in SKIP_PARTS:
            continue
        node: dict[str, Any] = {"name": name, "is_dir": is_dir}
        if is_dir:
            node["children"] = _scan(Path(e.path), f"{prefix}/{name}" if prefix else name)
        out.append(node)
    return out


def _render(entries: list[dict[str, Any]], indent: str) -> str:
    result = ""
    last = len(entries) - 1
    for i, entry in enumerate(entries):
        is_last = i == last
        branch = "└── " if is_last else "├── "
        next_indent = "    " if is_last else "│   "
        result += f"{indent}{branch}{entry['name']}{'/' if entry['is_dir'] else ''}\n"
        if entry["is_dir"] and entry.get("children"):
            result += _render(entry["children"], indent + next_indent)
    return result


def generate_directory_tree(workspace: str, max_context_tokens: int,
                            fraction: float = DEFAULT_DIRECTORY_TREE_FRACTION) -> str:
    max_tree_tokens = int(max_context_tokens * fraction)
    tree = _render(_scan(Path(workspace), ""), "")
    if estimate_tokens(tree) <= max_tree_tokens:
        return tree
    # Truncate to the lines that fit.
    lines = tree.split("\n")
    result = ""
    for idx, line in enumerate(lines):
        if estimate_tokens(result + line + "\n") > max_tree_tokens:
            result += f"... (truncated {len(lines) - idx} entries)\n"
            break
        result += line + "\n"
    return result


def directory_tree_block(workspace: str, max_context_tokens: int,
                         fraction: float = DEFAULT_DIRECTORY_TREE_FRACTION) -> str:
    tree = generate_directory_tree(workspace, max_context_tokens, fraction)
    if tree.strip():
        return (
            "[Workspace structure:\n"
            f"{tree}\n"
            ".gitignore patterns are respected.]"
        )
    return "[No workspace structure available]"
