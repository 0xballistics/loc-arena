"""The PR gate counts whole changed files and ratchets legacy sizes from the Git base."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[2] / "scripts" / "check_file_size.py"


def git(repository: Path, *arguments: str) -> str:
    """Run real Git commands against the isolated test repository."""
    return (
        subprocess.check_output(  # noqa: S603
            ["git", "-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid", *arguments],
            cwd=repository,
        )
        .decode()
        .strip()
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "commit", "--allow-empty", "--quiet", "-m", "base")
    return tmp_path


@pytest.mark.parametrize(
    ("before", "after", "fails"),
    [
        (None, 700, False),
        (None, 701, True),
        (699, 700, False),
        (699, 701, True),
        (700, 701, True),
        (810, 810, False),
        (810, 811, True),
        (810, 800, False),
        (810, 700, False),
    ],
)
def test_gate_applies_whole_file_cap_or_base_size_ratchet(
    repository: Path,
    before: int | None,
    after: int,
    fails: bool,
) -> None:
    path = repository / "changed.py"
    if before is not None:
        path.write_bytes(b"x\n" * before)
        git(repository, "add", path.name)
        git(repository, "commit", "--quiet", "-m", "original file")
    base = git(repository, "rev-parse", "HEAD")
    path.write_bytes(b"x\n" * (after - 1) + b"x")
    git(repository, "add", path.name)
    git(repository, "commit", "--quiet", "-m", "PR edit")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK), "--base", base],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == int(fails), result.stderr
    if fails:
        assert f"changed.py: {after} lines exceeds" in result.stdout


def test_gate_ignores_untouched_oversized_file_and_untracked_private_file(repository: Path) -> None:
    (repository / "legacy.py").write_bytes(b"x\n" * 1000)
    git(repository, "add", "legacy.py")
    git(repository, "commit", "--quiet", "-m", "legacy")
    (repository / "private.html").write_bytes(b"x\n" * 1000)
    (repository / "small.py").write_bytes(b"x\n")
    git(repository, "add", "small.py")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK), "--base", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["uv.lock", "image.png"])
def test_gate_excludes_generated_lock_and_binary_file(repository: Path, name: str) -> None:
    (repository / name).write_bytes((b"x\n" if name == "uv.lock" else b"\0\n") * 1000)
    git(repository, "add", name)

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK), "--base", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_gate_preserves_legacy_baseline_across_rename(repository: Path) -> None:
    (repository / "old.py").write_bytes(b"x\n" * 810)
    git(repository, "add", "old.py")
    git(repository, "commit", "--quiet", "-m", "legacy")
    git(repository, "mv", "old.py", "new.py")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(CHECK), "--base", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "change",
    [{"max_lines": True}, {"max_lines": 0}, {"excluded": "uv.lock"}, {"unexpected": 1}],
)
def test_gate_rejects_invalid_policy_at_startup(tmp_path: Path, change: dict[str, object]) -> None:
    script = tmp_path / CHECK.name
    script.write_text(CHECK.read_text(encoding="utf-8"), encoding="utf-8")
    policy = {"max_lines": 700, "excluded": []} | change
    (tmp_path / "file_size_limits.json").write_text(json.dumps(policy), encoding="utf-8")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Invalid file-size policy" in result.stdout


def test_gate_reads_non_ascii_policy_paths_independent_of_locale(repository: Path) -> None:
    script = repository / CHECK.name
    script.write_text(CHECK.read_text(encoding="utf-8"), encoding="utf-8")
    policy = {"max_lines": 700, "excluded": ["caf\u00e9.py"]}
    (repository / "file_size_limits.json").write_text(
        json.dumps(policy, ensure_ascii=False),
        encoding="utf-8",
    )
    (repository / "caf\u00e9.py").write_bytes(b"x\n" * 701)
    git(repository, "add", "caf\u00e9.py")

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script), "--base", "HEAD"],
        cwd=repository,
        env=os.environ | {"PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
