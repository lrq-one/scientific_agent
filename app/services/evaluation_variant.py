"""Server-side evaluation controls; never accepted from a user/API payload."""
import os

VARIANTS = {"FULL", "NO_SKILL", "NO_REPLAN", "STATELESS_FOLLOWUP", "NO_EVIDENCE_GATE"}
VARIANT = os.getenv("AGENT_EVALUATION_VARIANT", "FULL").upper()
if VARIANT not in VARIANTS:
    raise RuntimeError("Unknown evaluation variant")
if VARIANT != "FULL" and not (
    os.getenv("APP_MODE") == "evaluation" and os.getenv("AGENT_EVALUATION_ENABLED") == "1"
):
    raise RuntimeError("Ablations require an explicitly enabled evaluation server")

EVIDENCE_GATE_ENABLED = VARIANT != "NO_EVIDENCE_GATE"
