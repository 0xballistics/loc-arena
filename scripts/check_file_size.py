"""Check whole changed-file sizes against the PR base: 700 lines, or no legacy growth."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSizePolicy:
    """Validated limits without requiring CI to install the project's dependencies."""

    max_lines: int
    excluded: frozenset[str]


def load_policy(path: Path) -> FileSizePolicy:
    """Reject malformed policy fields, including booleans used as line limits."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"max_lines", "excluded"}:
        raise ValueError("invalid policy fields")
    maximum, excluded = raw["max_lines"], raw["excluded"]
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("max_lines must be a positive integer")
    if not isinstance(excluded, list) or any(not isinstance(name, str) or not name for name in excluded):
        raise ValueError("excluded must be a list of paths")
    return FileSizePolicy(maximum, frozenset(excluded))


def changed_files(ancestor: str) -> Iterator[tuple[str, str | None]]:
    """Yield current and original paths, preserving rename baselines and excluding deletions."""
    raw = subprocess.check_output(  # noqa: S603
        ["git", "diff", "--name-status", "--find-renames", "--diff-filter=ACMRT", "-z", ancestor, "--"],
    )
    fields = iter(raw.decode("utf-8").split("\0")[:-1])
    for status in fields:
        original = next(fields)
        name = next(fields) if status.startswith(("R", "C")) else original
        yield name, None if status.startswith(("A", "C")) else original


def line_count(data: bytes) -> int | None:
    """Count the whole UTF-8 text, including comments and blank lines; binary files have no line limit."""
    if b"\0" in data:
        return None
    try:
        return len(data.decode("utf-8").splitlines())
    except UnicodeDecodeError:
        return None


def main() -> int:
    """Check added/edited files against the merge ancestor, including tracked local edits."""
    try:
        policy = load_policy(Path(__file__).with_name("file_size_limits.json"))
    except (OSError, ValueError):
        print("Invalid file-size policy; check its paths and integer limits.")
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="origin/main", help="PR target ref or SHA; defaults to origin/main")
    base = parser.parse_args().base
    failures = 0
    try:
        ancestor = (
            subprocess.check_output(  # noqa: S603
                ["git", "merge-base", base, "HEAD"],
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )
        for name, original in changed_files(ancestor):
            path = Path(name)
            if name in policy.excluded or path.is_symlink() or not path.is_file():
                continue
            lines = line_count(path.read_bytes())
            if lines is None:
                continue
            before = (
                line_count(subprocess.check_output(["git", "show", f"{ancestor}:{original}"]))  # noqa: S603
                if original is not None
                else None
            )
            limit = max(policy.max_lines, before or 0)
            if lines > limit:
                print(f"{name}: {lines} lines exceeds {limit}; split the file into cohesive modules.")
                failures += 1
    except subprocess.CalledProcessError:
        print("Cannot determine changed files; fetch the PR base and pass it with --base.")
        return 1
    if failures:
        return 1
    print(f"Changed-file checks passed ({policy.max_lines} lines, or no growth above the base size).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
