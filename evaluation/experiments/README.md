# Six-track experiment package

This directory is a new, append-only experiment package. It does not overwrite
`evaluation/runs/`, frozen gold, historical tasks, PostgreSQL, or MinIO.

Each track contains `manifest.json`, `cases.jsonl`, `gold.jsonl`, baseline and
optimized metric snapshots, `paired.csv`, `ablation.json`, failure analysis and
a short report. `not_measured` is intentional when a fair paired run was not
available; it is never encoded as zero.

Run the package builder with:

```powershell
.\.venv\Scripts\python.exe evaluation/experiments/prepare_six_tracks.py
```

The current package uses existing reviewed fixtures and committed live traces.
No new Qwen call is made by the builder.
