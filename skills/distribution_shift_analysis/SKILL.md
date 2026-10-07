---
name: distribution_shift_analysis
description: Compare scientific feature or subgroup distributions across dataset versions, splits, or result files.
domains: [mass_spec, general]
intents: [database_analysis, mixed_analysis, file_analysis]
required_capabilities: [database]
optional_capabilities: [file, artifact]
risk_level: low
tags: [distribution, shift, drift, ood, train, test, 分布, 漂移, 偏移, ring_count, molecular_weight]
allowed_tools: [search_schema, get_table_schema, get_table_relationships, execute_readonly_sql, profile_dataset, group_metrics, join_tables, save_result_table, plot_metric_comparison]
---
# Purpose
Identify distribution changes that may explain performance differences without equating correlation with causation.
# When to Use
Use for train/test drift, version-to-version molecular composition changes, or subgroup proportion changes.
# When NOT to Use
Do not claim statistical significance unless the requested statistic and sample size support it.
# Preconditions
At least two comparable populations and a shared feature or subgroup definition.
# Required Inputs
Population identifiers, comparison feature(s), and optional output format.
# Procedure
Resolve both populations, retrieve or compute comparable summary distributions, align units/categories, quantify differences, and relate them to performance only when both Evidence sources exist.
# Tool Policy
Prefer deterministic aggregation; use exact join keys for cross-resource comparisons.
# Decision Rules
Report sample sizes and denominators. Missing categories are distinguished from zero counts.
# Evidence Requirements
Cite both population definitions, summaries, dataset versions, and any join key used.
# HITL Conditions
Ask which populations/splits to compare when ambiguous.
# Failure / Recovery
Do not broaden an empty query to another population without user approval.
# Stop Conditions
Stop after requested distribution differences and limitations are reported.
# Output Contract
Return comparable summaries, shift indicators, sample sizes, and uncertainty.
# Example Tasks
Compare ring_count and fused-ring proportions between train_v2 and train_v3.
