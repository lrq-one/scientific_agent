# Evaluation assets

This directory contains human-checked, deterministic JSONL cases for intent,
skill routing, tool routing, schema retrieval, Text-to-SQL, and end-to-end task
validation. `build_datasets.py` regenerates the committed cases; `validate.py`
checks counts and required fields. `metrics.py` implements metric functions only.

Phase 4 real-LLM results are recorded in `runs/`, with the frozen strategy in
`frozen_config.json`, aggregate metrics in `final_metrics.json`, and the reviewed
report in `../docs/phase4_evaluation_report.md`. The Test split has already been
consumed once for the frozen configuration and must not be reused for tuning.

The stage runners are `run_intent.py`, `run_skill.py`, `run_tool.py`,
`run_schema.py`, `run_text2sql.py`, `run_e2e.py`, and `run_hitl_resume.py`.
Each run writes `config.json`, `per_case.jsonl`, `metrics.json`, and
`errors.jsonl`. LLM runners require `LLM_API_BASE`, `LLM_API_KEY`, and
`LLM_MODEL=qwen3.7-flash`; database runners also require the read-only
`DATABASE_URL`. Never put the API key in a command-line argument or committed
configuration file.
