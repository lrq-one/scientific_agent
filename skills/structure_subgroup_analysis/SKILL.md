---
name: structure_subgroup_analysis
description: Compare metrics across interpretable molecular structure subgroups.
domains: [mass_spec]
intents: [file_analysis, mixed_analysis, scientific_model]
required_capabilities: [file]
optional_capabilities: [database, mcp, artifact]
risk_level: medium
tags: [subgroup, structure, 结构子群, fused_ring, cyclic, aromatic]
allowed_tools: [group_metrics, compare_structure_groups, get_molecule_features, join_tables, execute_readonly_sql, plot_metric_comparison, save_result_table, save_chart]
---
# Purpose
Measure performance differences across molecular structure groups.
# When to Use
Use for fused-ring, cyclic, aromatic, scaffold, or annotation-based comparisons.
# When NOT to Use
Do not claim group effects when group size or metadata quality is inadequate.
# Preconditions
Group labels and metric inputs are available or can be retrieved safely.
# Required Inputs
Molecule key, grouping definition, outcome/error field, and comparison scope.
# Procedure
Resolve features, construct groups, compute counts and metrics, compare, visualize, and document limitations.
# Tool Policy
All group assignments and values must come from registered tools or source data.
# Decision Rules
Report sample count beside every group metric; flag sparse groups.
# Evidence Requirements
Group claims cite definition, count, metric, source, and tool call.
# HITL Conditions
Ask when subgroup definitions overlap ambiguously.
# Failure / Recovery
Exclude unresolved records transparently and quantify exclusions.
# Stop Conditions
Stop when requested groups and statistically relevant caveats are covered.
# Output Contract
Return group table, chart, evidence, interpretation, and uncertainty.
# Example Tasks
Compare fused-ring and non-fused molecules for candidate-model error.
