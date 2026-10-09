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
