"""What a ``blind_monitor`` call takes from the monitors' view: its target's mirror events after the call.

The event builder (``loc_arena.recorded_events``) notes a drop for each such call it writes; once every event
of a phase is written, the mirror log loses each covered target's events after its earliest drop, while the
sealed log keeps them all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageDrop:
    """A blind_monitor call's effect on the mirror: the target's events after ``cut`` are dropped.

    ``cut`` is the blind_monitor action's ``(episode time, mirror seq)``: the mirror's own seq breaks a tie
    between events of the same clock step, which a time comparison alone would keep.
    """

    target: str
    cut: tuple[float, int]


def drop_covered_mirror_events(mirror_path: Path, drops: list[CoverageDrop]) -> None:
    """Drop from the mirror log at ``mirror_path`` each covered target's events after its coverage went.

    Keeps the sealed log whole; filters raw lines, so a kept event's bytes are unchanged, and leaves the file
    untouched when nothing is dropped (the deterministic run stays byte-identical).
    """
    cuts = _earliest_coverage_cuts(drops)
    if not cuts:
        return
    lines = mirror_path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if _mirror_keeps(line, cuts)]
    mirror_path.write_text("".join(f"{line}\n" for line in kept), encoding="utf-8")


def _earliest_coverage_cuts(drops: list[CoverageDrop]) -> dict[str, tuple[float, int]]:
    """Each target's cut: the ``(episode time, mirror seq)`` of its earliest blind_monitor call."""
    cuts: dict[str, tuple[float, int]] = {}
    for drop in drops:
        cuts[drop.target] = min(drop.cut, cuts.get(drop.target, drop.cut))
    return cuts


def _mirror_keeps(line: str, cuts: Mapping[str, tuple[float, int]]) -> bool:
    """Whether a mirror line survives: a covered target's event after its cut, by (ts, seq), is dropped."""
    event = json.loads(line)
    covered = cuts.get(str(event.get("actor_uid")))
    return covered is None or (float(event.get("ts", 0.0)), int(event.get("seq", 0))) <= covered
