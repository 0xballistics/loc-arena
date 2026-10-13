"""Enforce line limits for tracked UTF-8 text files, with bounded legacy exceptions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def main() -> int:
    """Report oversized tracked text files without reading ignored private artifacts."""
    policy = json.loads(Path(__file__).with_name("file_size_limits.json").read_text())
    tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")  # noqa: S603
    failures = 0
    for name in filter(None, tracked):
        path = Path(name)
        if name in policy["excluded"] or path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            lines = len(data.decode("utf-8").splitlines())
        except UnicodeDecodeError:
            continue
        limit = policy["existing_limits"].get(name, policy["max_lines"])
        if lines > limit:
            print(f"{name}: {lines} lines exceeds {limit}; split the file into cohesive modules.")
            failures += 1
    if failures:
        return 1
    print(f"File size checks passed (default limit: {policy['max_lines']} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
