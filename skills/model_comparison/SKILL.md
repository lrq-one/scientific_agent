---
name: model_comparison
description: Compare model predictions with reproducible overall and subgroup metrics.
domains: [mass_spec, general]
intents: [file_analysis, mixed_analysis]
required_capabilities: [file]
optional_capabilities: [database, artifact]
risk_level: low
tags: [比较, model_v1, model_v2, rt, mae, rmse, regression]
allowed_tools: [list_workspace_files, inspect_table, read_csv, read_excel, calculate_metrics, group_metrics, compare_models, find_high_error_samples, plot_metric_comparison, save_result_table, save_chart]
---
# Purpose
Compare two or more model result tables using deterministic calculations.
# When to Use
Use for model ranking, metric deltas, subgroup comparison, and regression review.
# When NOT to Use
Do not use for unsupported causal claims or when observed and predicted values are absent.
# Preconditions
Authorized files exist and model/dataset versions are identifiable.
# Required Inputs
Result tables, metric columns, model labels, and optional subgroup columns.
# Procedure
Inspect schemas, validate rows, compute comparable metrics, inspect subgroups, then create artifacts.
# Tool Policy
Use only allowed tools; calculations must run in code, never by language-model arithmetic.
# Decision Rules
Compare on matched samples; disclose unmatched rows and missing values.
# Evidence Requirements
Every numeric claim cites a file, calculation tool call, and dataset/model version when available.
# HITL Conditions
Ask when model identity, join key, or target column cannot be resolved safely.
# Failure / Recovery
Report validation failures; retry only with an explicitly corrected mapping.
# Stop Conditions
Stop after metrics, subgroup checks, evidence, and requested artifacts are complete.
# Output Contract
Return observed evidence, interpretation, uncertainty, a comparison table, and optional PNG chart.
# Example Tasks
Compare model_v1.csv and model_v2.csv and identify the largest subgroup regression.

