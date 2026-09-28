"""Small dependency-free benchmark harness for workflow wire formats.

This is intentionally scaffolding rather than a performance gate.  It gives
future format changes a repeatable command and records payload size, digest,
and mean serialization time without adding a benchmark dependency to the
plugin's locked runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from long_running_tasks.fixtures import sample_workflow_record
from long_running_tasks.workflow import WorkflowRecord, canonical_json
from long_running_tasks.workflow_run import WorkflowExecutionProjection


FORMAT_BENCHMARK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FormatMeasurement:
    name: str
    bytes: int
    sha256: str
    mean_nanoseconds: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "bytes": self.bytes,
            "mean_nanoseconds": self.mean_nanoseconds,
            "name": self.name,
            "sha256": self.sha256,
        }


def _serializers(record: WorkflowRecord) -> Mapping[str, Callable[[], str]]:
    projection = WorkflowExecutionProjection.from_workflow(
        record,
        "ticket-001",
        run_id="format-benchmark",
    )
    return {
        "workflow-json": record.to_json,
        "workflow-canonical-json": lambda: canonical_json(record.to_dict()),
        "execution-projection-json": projection.to_json,
    }


def benchmark_formats(
    record: WorkflowRecord | None = None,
    *,
    repeats: int = 100,
) -> dict[str, object]:
    """Measure stable serialization formats and return JSON-ready results."""

    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    selected = record or sample_workflow_record()
    measurements: list[FormatMeasurement] = []
    for name, serializer in _serializers(selected).items():
        payload = serializer().encode("utf-8")
        started = time.perf_counter_ns()
        for _ in range(repeats):
            serializer()
        elapsed = time.perf_counter_ns() - started
        measurements.append(
            FormatMeasurement(
                name=name,
                bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                mean_nanoseconds=elapsed // repeats,
            )
        )
    return {
        "schema_version": FORMAT_BENCHMARK_SCHEMA_VERSION,
        "record_digest": selected.digest,
        "repeats": repeats,
        "formats": [item.to_dict() for item in measurements],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args(argv)
    print(json.dumps(benchmark_formats(repeats=args.repeats), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
