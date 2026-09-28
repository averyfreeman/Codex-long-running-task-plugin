# Format benchmark scaffolding

The dependency-free harness compares the current canonical workflow JSON,
canonical compact JSON, and immutable execution-projection JSON formats. It
is intentionally observational: benchmark output is not a pass/fail gate and
does not write to the repository or runtime store.

Run it from the plugin directory with:

```sh
PYTHONPATH=src python3 benchmarks/format_benchmark.py --repeats 100
```

The JSON result records the fixture digest, payload byte count, payload
SHA-256, and mean serialization time. Future format work can add a serializer
to `benchmarks/format_benchmark.py` and compare the output without changing
the production workflow contract.
