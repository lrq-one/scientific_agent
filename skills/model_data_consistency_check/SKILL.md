---
name: model_data_consistency_check
description: Validate lineage consistency across model versions, runs, dataset versions, predictions, and annotations.
domains: [mass_spec, general]
intents: [database_analysis, mixed_analysis]
required_capabilities: [database]
optional_capabilities: [artifact]
risk_level: low
tags: [lineage, consistency, model_version, dataset_version, run, prediction, 版本, 一致性, 来源]
allowed_tools: [list_datasources, search_schema, get_table_schema, get_table_relationships, preview_table, execute_readonly_sql, save_result_table]
---
# Purpose
Check whether scientific outputs are traceable to the expected model, experiment, and dataset versions.
# When to Use
Use when a user questions which dataset trained a model, which run produced predictions, or whether version metadata is internally consistent.
# When NOT to Use
Do not infer lineage from names alone when the database does not contain an explicit relationship.
# Preconditions
Authorized read-only access to model, experiment, prediction, and dataset metadata.
# Required Inputs
The model/run/prediction/dataset identifiers that define the lineage question.
# Procedure
Retrieve schema and foreign-key relationships, query the explicit lineage path, compare expected and observed version identifiers, and preserve raw rows as Evidence.
# Tool Policy
Read-only database tools only; never repair metadata by writing to the source database.
# Decision Rules
A missing relationship is unknown, not consistent. A mismatch is reported with both observed identifiers.
# Evidence Requirements
Every consistency claim cites the relationship path, datasource, SQL parameters, and returned IDs/versions.
# HITL Conditions
Ask which model/run or dataset version is intended when multiple candidates remain.
# Failure / Recovery
Schema mismatch may trigger one bounded SQL repair; permission failures stop safely.
# Stop Conditions
Stop when the requested lineage is verified, contradicted, or cannot be established.
# Output Contract
Return lineage path, matched/mismatched identifiers, Evidence IDs, and unresolved gaps.
# Example Tasks
Check whether model_v3 predictions really came from an experiment trained on train_v2.
