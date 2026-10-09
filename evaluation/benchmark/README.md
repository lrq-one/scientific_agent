# Scientific Agent benchmark

This is a frozen, deterministic replay benchmark. `cases.jsonl` contains the
ten real acceptance scenarios; `gold.json` records the provenance and freeze
policy; `traces.baseline.jsonl` contains only facts already present in committed
isolated-integration evidence. The runner does not call Qwen/OpenAI and does
not access PostgreSQL or MinIO.

Run from the repository root:

```powershell
python evaluation/benchmark/run_benchmark.py --label baseline
```

Unknown persisted fields are reported as `null`/`not_measured`, never as zero.
Optimized and ablation runs must use the same case IDs and paired order. The
existing `evaluation/frozen_config.json` and historical `evaluation/runs/`
outputs are immutable; this directory is a new versioned benchmark artifact.

`cases_extended.jsonl` is the independent offline protocol suite (34 cases,
including scope recovery, SQL-candidate preconditions, goal contracts,
security, cancellation, UI projection and live-budget boundaries). Its labels
are provenance pointers and acceptance requirements, not model-generated gold.
It does not claim measured model quality. `live_qwen_runner.py` is the
explicitly authorized real-provider harness; it runs only synthetic fixtures,
does not execute SQL, and writes a separate immutable run directory containing
the manifest, per-stage telemetry, budget ledger and failure analysis.

Run `python evaluation/benchmark/run_extended_protocol.py` to validate the
34-case inventory. It reports every case as `measured: false` by policy; no
quality number is fabricated for cases without a recorded execution trace.
