# Prompt contract audit

Prompt contracts are versioned in `app/services/prompt_contract.py` for
planning/decision, Text2SQL, recovery, Skill routing and grounded response.
Existing safety instructions remain authoritative: QueryScope is not relaxed,
failed SQL is diagnostic-only, and GroundedResponse cites persisted Evidence.
No real-model accuracy improvement is claimed.
