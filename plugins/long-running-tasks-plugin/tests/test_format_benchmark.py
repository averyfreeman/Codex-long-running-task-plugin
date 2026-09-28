from __future__ import annotations

from benchmarks.format_benchmark import benchmark_formats


def test_format_benchmark_scaffolding_reports_stable_payloads():
    result = benchmark_formats(repeats=2)

    assert result["schema_version"] == 1
    assert result["repeats"] == 2
    formats = {item["name"]: item for item in result["formats"]}
    assert set(formats) == {
        "workflow-json",
        "workflow-canonical-json",
        "execution-projection-json",
    }
    for item in formats.values():
        assert item["bytes"] > 0
        assert len(item["sha256"]) == 64
        assert item["mean_nanoseconds"] >= 0
