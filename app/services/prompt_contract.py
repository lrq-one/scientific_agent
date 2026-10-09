"""Versioned prompt contracts used for audit and paired benchmark runs."""

PROMPT_VERSIONS = {
    "skill_routing": "skill-routing-v2-compact-metadata",
    "planning_decision": "decision-v3-scope-first",
    "recovery": "recovery-v2-directed-repair",
    "text2sql": "text2sql-v4-scope-candidate-lineage",
    "grounded_response": "grounded-response-v4-evidence-gate",
}

# The fragments are loaded by production nodes and hashed for experiment
# manifests.  Keeping this separate from node-specific prompt versions makes
# prompt changes auditable without duplicating the decision schemas.
PROMPT_CATALOG_VERSION = "prompt-catalog-v1"
