---
name: dataset_quality_audit
description: Audit tabular scientific datasets for completeness, consistency, duplicates, and leakage risks.
domains: [mass_spec, general]
intents: [file_analysis, database_analysis, mixed_analysis]
required_capabilities: [file]
optional_capabilities: [database, artifact]
risk_level: low
tags: [quality, audit, 质量, 缺失, 重复, leakage, 数据集]
allowed_tools: [inspect_table, read_csv, read_excel, profile_dataset, join_tables, filter_samples, preview_table, execute_readonly_sql, save_result_table]
---
# Purpose
Produce a reproducible dataset-quality audit.
# When to Use
Use for missingness, duplicate keys, invalid ranges, schema drift, and split leakage.
# When NOT to Use
Do not silently repair source data or infer undocumented domain constraints.
# Preconditions
Inputs are authorized and validation keys or constraints are known where required.
# Required Inputs
Tables, expected identifiers, target columns, and optional validity rules.
# Procedure
Inspect, profile, validate constraints, check joins/splits, quantify findings, and save a report table.
# Tool Policy
Use deterministic tabular tools; database access remains read-only.
# Decision Rules
Separate confirmed defects from warnings and state denominators.
# Evidence Requirements
Each finding includes count, affected field, source, and reproducible filter.
# HITL Conditions
Ask when a business rule or valid range materially changes the audit.
# Failure / Recovery
Continue independent checks if one field cannot be parsed; report the skipped check.
# Stop Conditions
Stop after scoped checks and an actionable evidence table are complete.
# Output Contract
Return severity-ranked findings, evidence, uncertainty, and CSV/XLSX artifact.
# Example Tasks
Audit a prediction dataset for missing values, duplicate molecules, and train/test leakage.
