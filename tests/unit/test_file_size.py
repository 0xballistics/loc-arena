"""The file-size gate checks tracked text, including the existing-file growth limits."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[2] / "scripts" / "check_file_size.py"


@pytest.mark.parametrize(
    ("name", "data", "tracked", "fails"),
    [
        ("new.py", b"x\n" * 700, True, False),
        ("new.py", b"x\n" * 700 + b"x", True, True),
        ("new.py", b"x\r\n" * 701, True, True),
        ("loc_arena/harness.py", b"x\n" * 810, True, False),
        ("loc_arena/harness.py", b"x\n" * 700, True, True),
        ("loc_arena/harness.py", b"x\n" * 699, True, True),
        ("loc_arena/harness.py", b"x\n" * 811, True, True),
        ("uv.lock", b"x\n" * 2000, True, False),
        ("image.png", b"\0\n" * 701, True, False),
        ("private.html", b"x\n" * 701, False, False),
    ],
)
def test_gate_rejects_only_tracked_text_above_its_limit(
    tmp_path: Path,
    name: str,
    data: bytes,
    tracked: bool,
    fails: bool,
) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)  # noqa: S603
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if tracked:
        subprocess.run(["git", "add", name], cwd=tmp_path, check=True)  # noqa: S603

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == int(fails), result.stderr
    if fails:
        assert name in result.stdout
        assert "exceeds" in result.stdout or "obsolete legacy ceiling" in result.stdout


@pytest.mark.parametrize(
    "change",
    [
        {"max_lines": True},
        {"max_lines": 0},
        {"excluded": "uv.lock"},
        {"existing_limits": {"legacy.py": False}},
        {"existing_limits": {"legacy.py": 700}},
        {"unexpected": 1},
    ],
)
def test_gate_rejects_invalid_policy_at_startup(tmp_path: Path, change: dict[str, object]) -> None:
    script = tmp_path / CHECK.name
    script.write_text(CHECK.read_text())
    policy = {"max_lines": 700, "excluded": [], "existing_limits": {}} | change
    (tmp_path / "file_size_limits.json").write_text(json.dumps(policy))

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Invalid file-size policy" in result.stdout
