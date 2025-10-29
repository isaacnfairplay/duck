#!/usr/bin/env python3
"""Bump the project version after merges based on change size."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

FILE_THRESHOLD = int(os.environ.get("FILE_THRESHOLD", "10"))
LINE_THRESHOLD = int(os.environ.get("LINE_THRESHOLD", "500"))
PYPROJECT_PATH = Path("pyproject.toml")


def _run_git_command(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc


def _determine_parent_commit() -> str | None:
    for candidate in ("HEAD^1", "HEAD^"):
        try:
            parent = _run_git_command("rev-parse", candidate)
        except RuntimeError:
            continue
        if parent:
            return parent
    return None


def _collect_diff_stats(parent: str) -> tuple[int, int]:
    output = _run_git_command("diff", "--shortstat", f"{parent}..HEAD")
    if not output:
        return 0, 0

    files_changed = 0
    insertions = 0
    deletions = 0

    files_match = re.search(r"(\d+) files? changed", output)
    if files_match:
        files_changed = int(files_match.group(1))

    insert_match = re.search(r"(\d+) insertions?\(\+\)", output)
    if insert_match:
        insertions = int(insert_match.group(1))

    delete_match = re.search(r"(\d+) deletions?\(-\)", output)
    if delete_match:
        deletions = int(delete_match.group(1))

    total_lines = insertions + deletions
    return files_changed, total_lines


def _extract_version(content: str) -> tuple[str, tuple[int, int, int]]:
    match = re.search(r"(?m)^\s*version\s*=\s*\"(\d+)\.(\d+)\.(\d+)\"", content)
    if not match:
        raise ValueError("Could not find version string in pyproject.toml")
    major, minor, patch = map(int, match.groups())
    return match.group(0), (major, minor, patch)


def _apply_new_version(content: str, old_line: str, new_version: str) -> str:
    new_line = re.sub(r"\"(\d+\.\d+\.\d+)\"", f'"{new_version}"', old_line, count=1)
    return content.replace(old_line, new_line, 1)


def main() -> int:
    if not PYPROJECT_PATH.exists():
        print("pyproject.toml not found; skipping version bump.")
        return 0

    parent = _determine_parent_commit()
    if not parent:
        print("No parent commit detected; skipping version bump.")
        return 0

    files_changed, total_lines = _collect_diff_stats(parent)
    if files_changed == 0 and total_lines == 0:
        print("No diff detected against parent commit; nothing to do.")
        return 0

    print(f"Detected diff: {files_changed} files changed, {total_lines} total line changes.")

    content = PYPROJECT_PATH.read_text()
    old_line, (major, minor, patch) = _extract_version(content)
    old_version = f"{major}.{minor}.{patch}"

    if files_changed > FILE_THRESHOLD or total_lines > LINE_THRESHOLD:
        minor += 1
        patch = 0
        bump_type = "minor"
    else:
        patch += 1
        bump_type = "patch"

    new_version = f"{major}.{minor}.{patch}"

    if new_version == old_version:
        print("Calculated version matches current version; nothing to update.")
        return 0

    print(f"Bumping version from {old_version} to {new_version} ({bump_type} change).")

    new_content = _apply_new_version(content, old_line, new_version)
    if new_content == content:
        print("Version unchanged after processing; nothing to write.")
        return 0

    PYPROJECT_PATH.write_text(new_content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
