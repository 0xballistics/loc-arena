"""Run the throughput/cost benchmark: ``python -m meridian_evalkit.bench [--requests N] [--rounds R]``.

Each round serves a fresh workload of ``--requests`` requests, seeded ``--seed`` plus the round's number, and
prints its :class:`BenchResult` as one JSON line as soon as it is served, so a long sweep shows its progress.
"""

from __future__ import annotations

import argparse
import dataclasses
import json

from meridian_evalkit.bench.throughput import ThroughputBench
from meridian_evalkit.bench.workload import build_workload


def main(arguments: list[str] | None = None) -> None:
    """Serve ``--rounds`` workloads through one serving engine and print each round's result."""
    parser = argparse.ArgumentParser(prog="python -m meridian_evalkit.bench", description=__doc__)
    parser.add_argument("--requests", type=int, default=64, help="requests in each round's workload")
    parser.add_argument("--rounds", type=int, default=1, help="workloads to serve, one after another")
    parser.add_argument("--seed", type=int, default=0, help="the first round's workload seed")
    options = parser.parse_args(arguments)
    bench = ThroughputBench()
    for round_number in range(options.rounds):
        result = bench.run(build_workload(options.requests, seed=options.seed + round_number))
        print(json.dumps({"round": round_number, **dataclasses.asdict(result)}), flush=True)


if __name__ == "__main__":
    main()
