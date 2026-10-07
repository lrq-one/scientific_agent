---
name: rt_prediction_review
description: Review retention-time predictions, model provenance, and applicability constraints.
domains: [mass_spec]
intents: [scientific_model, file_analysis, mixed_analysis]
required_capabilities: [scientific_model]
optional_capabilities: [file, database, mcp, artifact]
risk_level: medium
tags: [predict_rt, 预测保留时间, retention, applicability, model_version]
allowed_tools: [predict_rt, get_molecule_features, calculate_metrics, group_metrics, execute_readonly_sql, save_result_table]
---
# Purpose
Review RT predictions with explicit model and applicability provenance.
# When to Use
Use for prediction requests or review of supplied prediction outputs.
# When NOT to Use
Do not present a synthetic or unconfigured model as a real scientific predictor.
# Preconditions
Model availability, molecule representation, and units are known.
# Required Inputs
SMILES or molecule IDs, model version, expected units, and optional observations.
# Procedure
Validate inputs, retrieve features, call the registered model, compare observations if present, and assess applicability.
# Tool Policy
Only registered scientific model tools may make predictions.
# Decision Rules
Clearly label unavailable weights, out-of-domain inputs, and synthetic outputs.
# Evidence Requirements
Predictions cite model version, input identity, tool call, and uncertainty metadata.
# HITL Conditions
Ask when stereochemistry, units, or model choice is ambiguous.
# Failure / Recovery
Return validation evidence without a prediction if model execution is unavailable.
# Stop Conditions
Stop after predictions and review caveats are complete.
# Output Contract
Return prediction table, model provenance, applicability notes, and uncertainty.
# Example Tasks
Review RT predictions for the uploaded molecules using a configured model version.
