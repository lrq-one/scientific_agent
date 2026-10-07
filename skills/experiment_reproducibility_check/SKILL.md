---
name: experiment_reproducibility_check
description: Check whether scientific experiment inputs, versions, runs, and outputs are reproducible.
domains: [mass_spec, general]
intents: [database_analysis, mixed_analysis, file_analysis]
required_capabilities: [database]
optional_capabilities: [file, artifact]
risk_level: low
tags: [reproducibility, experiment, provenance, 可复现, 实验, version]
allowed_tools: [list_datasources, search_schema, get_table_schema, get_table_relationships, inspect_table, execute_readonly_sql, join_tables, save_result_table]
---
# Purpose
Verify that an experiment has traceable data, model, parameters, run, and outputs.
# When to Use
Use for provenance reviews, rerun readiness, and experiment handoff.
# When NOT to Use
Do not certify scientific validity; this skill checks traceability and repeatability inputs.
# Preconditions
Relevant experiment records or manifests are accessible.
# Required Inputs
Experiment identity and expected reproducibility requirements.
# Procedure
Trace dataset version, model version, run metadata, metrics, files, and missing dependencies.
# Tool Policy
Inspect only authorized resources; do not mutate experiment records.
# Decision Rules
Mark each required element present, missing, ambiguous, or inconsistent.
# Evidence Requirements
Every checklist result cites its record/file and identifier.
# HITL Conditions
Ask when multiple runs match the same informal experiment name.
# Failure / Recovery
Return a partial checklist with inaccessible dependencies explicitly named.
# Stop Conditions
Stop when all scoped checklist items have a recorded status.
# Output Contract
Return reproducibility checklist, evidence, blockers, and result table artifact.
# Example Tasks
Check whether the RT comparison experiment can be rerun from stored metadata.
