---
name: spectrum_quality_analysis
description: Audit persisted MS/MS spectra metadata and peak payload quality without inventing spectral interpretation.
domains: [mass_spec]
intents: [database_analysis]
required_capabilities: [database]
optional_capabilities: [artifact]
risk_level: low
tags: [msms, spectrum, spectra, peak, precursor_mz, collision, 谱图, 谱峰, 质谱质量]
allowed_tools: [search_schema, get_table_schema, get_table_relationships, preview_table, execute_readonly_sql, save_result_table]
---
# Purpose
Detect basic MS/MS data-quality issues using stored precursor metadata and peak payloads.
# When to Use
Use for missing/empty peak lists, implausible metadata, version coverage, and spectrum-count audits.
# When NOT to Use
Do not perform peak assignment, fragmentation interpretation, or collision-energy analysis when those fields/tools are absent.
# Preconditions
Authorized access to msms_spectra and related dataset/molecule metadata.
# Required Inputs
Dataset/version or molecule scope and the requested quality checks.
# Procedure
Retrieve schema first, inspect the requested scope, count spectra, detect empty/null peak payloads and missing metadata, and preserve representative rows.
# Tool Policy
Read-only SQL only; JSON payload inspection must remain bounded.
# Decision Rules
No spectra returned means NO_DATA for the scope, not proof that spectra do not exist globally.
# Evidence Requirements
Cite dataset scope, spectrum IDs/counts, precursor fields, and bounded peak-payload checks.
# HITL Conditions
Ask for dataset/version when multiple scopes could materially change the audit.
# Failure / Recovery
Stop safely on unauthorized or malformed JSON sources; do not silently skip them.
# Stop Conditions
Stop after requested QC checks and unresolved schema limitations are reported.
# Output Contract
Return QC counts, flagged records, scope, and limitations.
# Example Tasks
Audit train_v3 MS/MS records for empty peak arrays and missing precursor metadata.
