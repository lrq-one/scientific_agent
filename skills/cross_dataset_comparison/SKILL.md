---
name: cross_dataset_comparison
description: Compare observed molecular structure coverage between two explicit training dataset versions.
domains: [mass_spec, general]
intents: [database_analysis]
required_capabilities: [database]
optional_capabilities: [artifact]
risk_level: low
tags: [比较, 对比, compare, 数据集, 版本, training_db, 结构类型]
requires_two_dataset_versions: true
allowed_tools: [get_table_schema, execute_readonly_sql, save_result_table]
---
# Purpose
Compare observed sample counts and molecular structure composition for two explicit training versions.
# When to Use
Use when the user explicitly compares two training dataset versions by molecular structure or sample coverage.
# When NOT to Use
Do not use for a single version, two model files, model performance, or unapproved data.
# Preconditions
Confirm the authorized datasource, read-only DB role and required table/column schema.
# Required Inputs
An authorized datasource and exactly two distinct `train_vN` versions in the user's request.
# Data Dependencies
`training_molecules(dataset_version, molecule_id)` and `molecules(molecule_id, structure_type)`.
# Procedure
Check authorization and required schema, validate one parameterized read-only grouped SQL query with SQLGlot and database EXPLAIN, execute it, then calculate per-structure deltas from the returned rows.
# Tool Policy
Use `get_table_schema`, SQL checker and `execute_readonly_sql`; never execute writes.
# Decision Rules
Both versions must return rows before calculating a comparison. Derive shares using observed per-version denominators.
# Prohibited Conclusions
Do not infer data quality, unique molecular coverage, distribution shift, model performance, or causal undercoverage from these counts alone. Do not treat a missing version or zero returned rows as a scientific absence.
# Evidence Requirements
Preserve the SQL, bound versions, complete bounded raw rows, source and tool call ID. Report counts and deltas only when both versions are present. State unsupported requested dimensions explicitly.
# HITL Conditions
Ask for a second version if it is missing; do not infer one from conversation history without a resolved task link.
# Failure / Recovery
Fail safely on missing schema, authorization, query validation or execution failure. Never broaden the query or run writes.
# Stop Conditions
Stop after the bounded two-version query and evidence-backed comparison.
# Output Contract
Return source, both versions, structure counts, per-version denominators and shares, deltas, Evidence IDs and uncertainty.
# Example Tasks
Compare structure-type coverage in `training_db` between `train_v2` and `train_v3`.
