# Scientific Agent quantitative benchmark

The frozen benchmark is `evaluation/benchmark/`. It contains ten real
acceptance scenarios (A, B, C, D06, D09, M02, D08, HITL, RERUN and CANCEL),
labels sourced from committed evidence, and a deterministic replay runner.
Existing frozen configuration and historical `evaluation/runs/` outputs are not
modified.

The baseline was recorded at Git SHA `209fda8ecc15f130260b28af3e0dfdeb5fe6f9b4`
in `offline_replay`: task success 1.0, Evidence coverage 1.0, tool success
1.0, average known tool calls 5.2, average known decision calls 2.8, average
known replans 0.6 and known invalid ToolCalls 0. Token and latency remain
`not_measured` because historical traces did not persist those fields.

```powershell
python evaluation/benchmark/run_benchmark.py --label baseline
python evaluation/benchmark/compare_runs.py evaluation/benchmark/results/baseline.json evaluation/benchmark/results/optimized.json
```

The comparator refuses mismatched case IDs/order. A model-quality claim requires
an actual paired run with identical inputs, budget and model.

`results/optimized.json` and `results/ablation.json` are protocol replays of the
same committed trace, included to exercise the paired-report path. They do not
claim a live optimization delta; a real optimized/ablation trace must replace
them after an authorized, isolated run.
