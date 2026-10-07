---
name: scientific_result_summary
description: Synthesize completed scientific task evidence into a bounded result summary.
domains: [mass_spec, general]
intents: [file_analysis, database_analysis, mixed_analysis, scientific_model, general]
required_capabilities: []
optional_capabilities: [file, database, scientific_model, mcp, artifact]
risk_level: low
tags: [summary, report, 总结, 结论, evidence, artifact]
allowed_tools: [save_result_table, save_chart]
---
# Purpose
Summarize verified results without adding unsupported facts.
# When to Use
Use at task completion or when a concise evidence-led report is requested.
# When NOT to Use
Do not substitute for missing analyses or silently invent numeric values.
# Preconditions
Task evidence and provenance are available.
# Required Inputs
Goal, completed plan steps, evidence records, uncertainties, and artifacts.
# Procedure
Organize observed evidence, interpretation, uncertainty, and artifact links.
# Tool Policy
No new analysis tools unless explicitly planned; artifact tools may format existing evidence.
# Decision Rules
Prefer source-backed claims and separate observation from interpretation.
# Evidence Requirements
Every material numeric statement maps to a stored evidence item.
# HITL Conditions
Ask only when the requested reporting scope is ambiguous and materially consequential.
# Failure / Recovery
Omit unsupported claims and state which evidence is missing.
# Stop Conditions
Stop once the result is concise, traceable, and answers the goal.
# Output Contract
Return Observed Evidence, Interpretation, Uncertainty, and Artifacts sections.
# Example Tasks
Summarize the completed model comparison for a research review.
