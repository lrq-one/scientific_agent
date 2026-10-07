---
name: training_coverage_analysis
description: Inspect versioned training coverage for molecular structures and subgroups.
domains: [mass_spec, general]
intents: [database_analysis, mixed_analysis]
required_capabilities: [database]
optional_capabilities: [mcp, artifact]
risk_level: low
tags: [training_db, 训练覆盖, coverage, 样本, dataset_version, split]
allowed_tools: [list_datasources, search_schema, get_table_schema, get_table_relationships, preview_table, execute_readonly_sql, get_molecule_features, save_result_table]
---
# Purpose
Measure training membership coverage by dataset version, split, and molecular subgroup.
# When to Use
Use for representation gaps, subgroup counts, split leakage checks, and version comparisons.
# When NOT to Use
Do not use against unauthorized data sources or without a resolvable dataset version.
# Preconditions
Datasource authorization and read-only schema access are confirmed.
# Required Inputs
Datasource, dataset version, grouping dimension, and optional target molecules.
# Procedure
Retrieve schema, inspect relationships, generate guarded SQL, execute, and summarize coverage.
# Tool Policy
Only read-only SQL is permitted; retrieve schema before generation.
# Decision Rules
Distinguish zero coverage from missing metadata and report denominators.
# Evidence Requirements
Counts cite datasource, dataset version, SQL tool call, and relationship path.
# HITL Conditions
Ask for dataset version when several valid versions would change the result.
# Failure / Recovery
Repair rejected SQL only within retrieved tables and the original analytical goal.
# Stop Conditions
Stop after requested coverage and material gaps are reported.
# Output Contract
Return grouped counts, version context, query provenance, and uncertainty.
# Example Tasks
Check whether fused-ring molecules are underrepresented in train_v3.

