---
name: mass_spec_error_analysis
description: Diagnose retention-time or spectrum prediction errors across molecular groups.
domains: [mass_spec]
intents: [file_analysis, mixed_analysis, scientific_model]
required_capabilities: [file]
optional_capabilities: [database, mcp, scientific_model, artifact]
risk_level: medium
tags: [质谱, rt, fused, cyclic, 分子, 误差, smiles]
allowed_tools: [inspect_table, profile_dataset, calculate_metrics, group_metrics, find_high_error_samples, get_molecule_features, predict_rt, compare_structure_groups, execute_readonly_sql, save_result_table, save_chart]
---
# Purpose
Locate and characterize high-error molecular observations without overstating causality.
# When to Use
Use for RT/MS-MS residuals, fused-ring or cyclic subgroup errors, and outlier inspection.
# When NOT to Use
Do not infer mechanisms from correlation alone or invoke unconfigured real model weights.
# Preconditions
Prediction and observation columns exist; molecule identifiers are traceable.
# Required Inputs
Prediction table, error target, molecule key, and optional structure metadata.
# Procedure
Validate data, compute residual metrics, rank errors, enrich structures, compare groups, and inspect coverage.
# Tool Policy
Prefer deterministic file/database tools and registered MCP tools; preserve resource boundaries.
# Decision Rules
Flag small groups and missing training coverage before interpreting group differences.
# Evidence Requirements
Claims include computed values, source records, tool call IDs, and data/model versions.
# HITL Conditions
Ask for ambiguous molecule keys, units, or dataset versions.
# Failure / Recovery
On enrichment failure, retain file evidence and mark molecular interpretation unavailable.
# Stop Conditions
Stop when major outliers and group patterns are characterized with uncertainty.
# Output Contract
Return evidence, bounded interpretation, uncertainty, and reproducible result artifacts.
# Example Tasks
Analyze why fused-ring molecules have higher RT prediction error.

