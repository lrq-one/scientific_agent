# Scientific model integration: blocked resource audit and adapter contract

Checked 2026-10-07. `app/tools/scientific_model.py:predict_rt` always returns a failure, including when `MODEL_PATH` is set (`model_adapter_not_implemented`). The previous `ResourceService.discover` advertised `rt_model` when `MODEL_PATH` was merely nonempty; this false capability advertisement is now disabled pending a working adapter. No TC-TopoRT or chenmt-ms checkpoint, corresponding preprocessing code/config, model-version manifest, or licensed inference package was found under `D:\工作\scientific_agent` or elsewhere under `D:\工作`. The only discovered `.safetensors` outside this project belongs to a Chinese text embedding model in Aix-DB; it is unrelated. No weights were downloaded and no GPU job was started.

Before an adapter can be implemented and claimed as real:

1. Supply an exact model family and immutable version, checkpoint path/hash, loading library/version and license.
2. Supply the training-time preprocessing contract: SMILES normalization, salt/stereo policy, tokenization or molecular graph featurization, missing/invalid structure handling, and any chromatography condition features.
3. Define input schema (`smiles`, conditions, optional batch), output schema (RT units, uncertainty/confidence, per-item errors), supported device and memory/time limits.
4. Implement a startup capability check that actually loads the checkpoint and runs a known test vector; only then advertise `scientific_model` in `ResourceSummary`.
5. Bound batch size and timeout, record model/checkpoint/preprocessing hashes in Evidence and artifacts, and distinguish unavailable capability from inference failure.
6. Add golden-vector, invalid-input, CPU resource-limit, version mismatch and provenance tests. Do not use fabricated demo predictions as scientific-model results.

Until these dependencies are provided, scientific-model inference remains `NOT_IMPLEMENTED`; the v2 scientific-inference family is blocked, not counted as a success or silently excluded from coverage reporting.
