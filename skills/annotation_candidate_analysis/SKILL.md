---
name: annotation_candidate_analysis
description: Analyze metabolite annotation candidates using persisted structural, RT, MS/MS, and annotation evidence.
domains: [mass_spec]
intents: [database_analysis, mixed_analysis]
required_capabilities: [database]
optional_capabilities: [mcp, artifact]
risk_level: low
tags: [annotation, candidate, metabolite, rt, msms, structure, 候选, 注释, 代谢物, 质谱]
allowed_tools: [search_schema, get_table_schema, get_table_relationships, preview_table, execute_readonly_sql, get_molecule_features, save_result_table]
---
# Purpose
Assemble and compare evidence for metabolite annotation candidates without fabricating unavailable spectral or model scores.
# When to Use
Use when candidates, annotations, RT measurements, spectra, or molecular metadata are present in authorized sources.
# When NOT to Use
Do not claim candidate ranking from model scores that were not actually computed or stored.
# Preconditions
A candidate set or resolvable target molecule and at least one persisted evidence source.
# Required Inputs
Target/candidate identifiers and the evidence dimensions requested by the user.
# Procedure
Resolve candidates, retrieve available annotation/RT/MS/MS/structure evidence, align by molecule_id, compare only observed evidence, and flag ties or missing dimensions.
# Tool Policy
Use exact molecule identifiers for joins; MCP enrichment is optional and must not replace persisted evidence silently.
# Decision Rules
Missing evidence lowers confidence but is not treated as a negative observation.
# Evidence Requirements
Every candidate conclusion links to molecule_id and the concrete RT/MS/MS/annotation rows used.
# HITL Conditions
Ask for the candidate set or target when it cannot be inferred safely.
# Failure / Recovery
If one evidence source fails, preserve the others and state that the comparison is partial.
# Stop Conditions
Stop when the requested candidates are compared or evidence is insufficient.
# Output Contract
Return per-candidate evidence, unresolved dimensions, and provenance; avoid unsupported final identification.
# Example Tasks
Compare candidate molecules using stored RT and MS/MS annotation evidence and explain why one remains ambiguous.
