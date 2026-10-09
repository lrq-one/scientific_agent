# Skill Routing + Prompt + Context

Cases: 150; split: dev+test_source_fixture.
Baseline: unrecorded historical phase4 run (all-catalog routing and inline prompts).
Optimized code: app/services/skills.py; app/services/prompt_catalog.py; app/services/context_projection.py.

## Measurement boundary

Historical Skill runs are referenced, not rerun. Current prompt/catalog/context changes are validated offline; no current paired Qwen delta is claimed.

## Limitations

- Historical skill configs do not persist a Git SHA for every run.
- Current six-track pairing has zero new provider calls; live quality/cost remains not_measured.

## Failure analysis

- BM25 k=3 reduced input tokens but lowered retrieval recall in the existing 100-case run.
- No-skill live ablation matched optimized strict success on 10 cases, so Skill benefit is unproven.
