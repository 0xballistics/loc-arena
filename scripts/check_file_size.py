"""Enforce line limits for tracked UTF-8 text files, with bounded legacy exceptions."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileSizePolicy:
    """Validated limits without requiring CI to install the project's dependencies."""

    max_lines: int
    excluded: frozenset[str]
    existing_limits: dict[str, int]


def load_policy(path: Path) -> FileSizePolicy:
    """Reject malformed policy fields, including booleans used as line limits."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict) or set(raw) != {"max_lines", "excluded", "existing_limits"}:
        raise ValueError("invalid policy fields")
    maximum, excluded, existing = raw["max_lines"], raw["excluded"], raw["existing_limits"]
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("max_lines must be a positive integer")
    if not isinstance(excluded, list) or any(not isinstance(name, str) or not name for name in excluded):
        raise ValueError("excluded must be a list of paths")
    if not isinstance(existing, dict):
        raise ValueError("existing_limits must map paths to integer limits")
    limits: dict[str, int] = {}
    for name, limit in existing.items():
        if not isinstance(name, str) or not name or type(limit) is not int or limit <= maximum:
            raise ValueError("legacy limits must be integers greater than max_lines")
        limits[name] = limit
    return FileSizePolicy(maximum, frozenset(excluded), limits)


def main() -> int:
    """Report oversized tracked text files without reading ignored private artifacts."""
    try:
        policy = load_policy(Path(__file__).with_name("file_size_limits.json"))
    except (OSError, ValueError):
        print("Invalid file-size policy; check its paths and integer limits.")
        return 1
    tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")  # noqa: S603
    failures = 0
    for name in filter(None, tracked):
        path = Path(name)
        if name in policy.excluded or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            lines = len(data.decode("utf-8").splitlines())
        except UnicodeDecodeError:
            continue
        limit = policy.existing_limits.get(name, policy.max_lines)
        if name in policy.existing_limits and lines <= policy.max_lines:
            print(f"{name}: remove its obsolete legacy ceiling; file now has {lines} lines.")
            failures += 1
        elif lines > limit:
            print(f"{name}: {lines} lines exceeds {limit}; split the file into cohesive modules.")
            failures += 1
    if failures:
        return 1
    print(f"File size checks passed (default limit: {policy.max_lines} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
