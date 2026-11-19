"""Mesure reproductible de l'index et des traces.

Sans mesure, une régression de compaction ou de reconstruction passe inaperçue
jusqu'à ce que l'interface mette plusieurs secondes à s'ouvrir. Ce script
génère un index synthétique (plusieurs états par run), des traces terminées et
un dossier d'atelier, puis chronomètre les opérations de maintenance et le
diagnostic.

    python tools/benchmark_traces.py --runs 100000 --traces 2000

Aucun réseau, aucun provider, aucun fichier utilisateur : tout est sous un
dossier temporaire, supprimé à la fin. Une durée dépassant `--max-seconds`
(retardée par `BENCHMARK_MAX_FACTOR` pour une machine lente) fait échouer le
script, ce qui permet de l'appeler depuis la CI nocturne.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.index_doctor import diagnose  # noqa: E402
from core.run_index import RunIndex  # noqa: E402
from core.trace import read_events, workspace_slug  # noqa: E402


def _seed(root: Path, *, runs: int, traces: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    directory = root
    records = []
    for index in range(runs):
        run_id = f"run-{index:06d}"
        records.append(
            {
                "run_id": run_id,
                "state": "started" if index % 3 else "finished",
                "path": str(directory / f"{run_id}.jsonl"),
                "started": "2026-01-01T00:00:00+00:00",
                "updated": f"2026-01-01T00:{index // 60000:02d}:{index % 60:02d}+00:00",
                "model": "bench-model",
                "workspace": "bench",
                "trace_format": 3,
                "ok": index % 2 == 0,
                "bytes": 100,
            }
        )
        if index % 5 == 0:
            records.append({**records[-1], "state": "finished"})
    (directory / "runs.index").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    for index in range(traces):
        run_id = f"trace-{index:06d}"
        lines = [
            {
                "run_id": run_id,
                "type": "run_started",
                "seq": 1,
                "payload": {"model": "bench-model", "workspace": "bench", "trace_format": 3},
            },
            {
                "run_id": run_id,
                "type": "run_finished",
                "seq": 2,
                "payload": {"ok": True, "stop_reason": "final"},
            },
        ]
        (directory / f"{run_id}.jsonl").write_text(
            "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
        )
    workspace = directory / workspace_slug(root)
    workspace.mkdir(exist_ok=True)
    (workspace / "atelier.jsonl").write_text(
        json.dumps(
            {
                "run_id": "atelier",
                "type": "run_started",
                "seq": 1,
                "payload": {"model": "m", "workspace": "atelier", "trace_format": 3},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    RunIndex(workspace).mark("atelier", "finished", path=workspace / "atelier.jsonl")


def run_benchmark(*, runs: int = 20_000, traces: int = 500, max_seconds: float = 20.0) -> dict[str, float]:
    """Exécute les mesures et retourne les durées (secondes)."""
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="omniagent-bench-") as tmp:
        root = Path(tmp)
        started = time.perf_counter()
        _seed(root, runs=runs, traces=traces)
        timings["seed"] = round(time.perf_counter() - started, 3)
        index = RunIndex(root)
        started = time.perf_counter()
        index.entries()
        timings["entries"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        index.compact()
        timings["compact"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        index.rebuild(max_files=traces, reset=True)
        timings["rebuild"] = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        diagnose(root)
        timings["doctor"] = round(time.perf_counter() - started, 3)
        sample = root / "trace-000000.jsonl"
        started = time.perf_counter()
        for _ in range(100):
            list(read_events(sample))
        timings["read_100"] = round(time.perf_counter() - started, 3)
    factor = float(os.environ.get("BENCHMARK_MAX_FACTOR", "1") or 1)
    timings["threshold"] = round(max_seconds * factor, 3)
    return timings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20_000)
    parser.add_argument("--traces", type=int, default=500)
    parser.add_argument("--max-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)
    timings = run_benchmark(
        runs=max(1, args.runs),
        traces=max(1, args.traces),
        max_seconds=max(0.1, args.max_seconds),
    )
    for name, value in timings.items():
        print(f"{name.ljust(10)} {value} s")
    over = [
        name
        for name, value in timings.items()
        if name != "threshold" and value > timings["threshold"]
    ]
    if over:
        print(f"Depassement du seuil pour : {', '.join(over)}", file=sys.stderr)
        return 1
    print("Benchmark dans le seuil.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
