---
name: experiment_run_diagnosis
description: Diagnose why one scientific model run differs from another using experiment, model, dataset, and metric metadata.
domains: [mass_spec, general]
intents: [database_analysis]
required_capabilities: [database]
optional_capabilities: [artifact]
risk_level: low
tags: [experiment, run, metrics, regression, seed, checkpoint, 实验, 运行, 指标, 退化]
allowed_tools: [search_schema, get_table_schema, get_table_relationships, preview_table, execute_readonly_sql, save_result_table]
---
# Purpose
Trace performance changes to observable run, model-version, dataset-version, or metric differences.
# When to Use
Use when comparing experiment runs or investigating a regression between model runs.
# When NOT to Use
Do not invent hyperparameters or seeds that are not stored in experiment metadata.
# Preconditions
The compared runs can be identified in the authorized metadata store.
# Required Inputs
At least one target run and preferably a baseline/comparison run.
# Procedure
Resolve runs, follow experiment/model/dataset relationships, compare stored metrics and metadata, then separate observed differences from hypotheses.
# Tool Policy
Read-only SQL only; no training job mutation or checkpoint modification.
# Decision Rules
Only observed metadata differences become supported claims; causal explanations remain hypotheses unless directly evidenced.
# Evidence Requirements
Cite run IDs, model versions, dataset versions, metrics rows, and SQL provenance.
# HITL Conditions
Ask for the intended baseline run if several plausible comparisons exist.
# Failure / Recovery
If a run cannot be resolved, stop with INSUFFICIENT_EVIDENCE instead of selecting a similarly named run.
# Stop Conditions
Stop after observable run differences and unresolved causes are enumerated.
# Output Contract
Return run comparison, supported differences, hypotheses, and Evidence links.
# Example Tasks
Why did experiment run B perform worse than run A, and did the dataset version change?
