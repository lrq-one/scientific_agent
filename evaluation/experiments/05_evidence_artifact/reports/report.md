# Evidence + Artifact

Cases: 34; split: replay_protocol.
Baseline: 1802835 (pre-evidence/artifact guard lineage).
Optimized code: app/services/grounded_response.py; app/services/artifacts.py.

## Measurement boundary

Existing grounded-response and isolated MinIO tests are the evidence. Artifact byte hashes/columns are now persisted in generated artifact metadata; no production object was touched.

## Limitations

- Independent 34-case evidence claim adjudication is not measured in this batch.
- MinIO verification metrics remain isolated-test only.

## Failure analysis

- Unsupported claims are rejected by persisted Evidence IDs.
- A failed export remains non-artifact; generated outputs now carry byte and column metadata for verification.
