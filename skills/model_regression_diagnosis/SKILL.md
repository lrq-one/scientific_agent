---
name: model_regression_diagnosis
description: Diagnose where a candidate model regresses against a baseline.
domains: [mass_spec, general]
intents: [file_analysis, mixed_analysis]
required_capabilities: [file]
optional_capabilities: [database, artifact]
risk_level: medium
tags: [regression, baseline, candidate, 回归退化, 新模型, delta]
allowed_tools: [inspect_table, compare_models, find_high_error_samples, group_metrics, join_tables, filter_samples, plot_metric_comparison, save_result_table, save_chart]
---
# Purpose
Identify samples and subgroups where a candidate is worse than a baseline.
# When to Use
Use for release gates, model-version comparisons, and targeted regression analysis.
# When NOT to Use
Do not compare unmatched evaluations without a defensible alignment key.
# Preconditions
Baseline and candidate outputs share target semantics and test scope.
# Required Inputs
Two result sources, sample key, target/observed column, and acceptance criteria.
# Procedure
Align samples, compute paired deltas, rank regressions, group them, and create decision artifacts.
# Tool Policy
Use code-based paired calculations; preserve input files unchanged.
# Decision Rules
Distinguish overall improvement from subgroup regression and apply supplied thresholds.
# Evidence Requirements
Every regression cites sample/group, paired delta, versions, and source.
# HITL Conditions
Ask if evaluation populations or metric direction differ.
# Failure / Recovery
Fall back to intersection-only comparison and disclose coverage loss.
# Stop Conditions
Stop after threshold breaches and largest regressions are explained as far as evidence allows.
# Output Contract
Return release-oriented summary, paired evidence table, chart, and uncertainty.
# Example Tasks
Find regressions in model_v2 versus model_v1 and group them by structure type.
