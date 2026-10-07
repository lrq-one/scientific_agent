from __future__ import annotations

import os
from app.models.schemas import ToolResult


def predict_rt(smiles: str) -> ToolResult:
    model_path = os.getenv("MODEL_PATH")
    if not model_path:
        return ToolResult(success=False, source="rt_model", error="capability_unavailable", metadata={"reason": "MODEL_PATH is not configured"})
    return ToolResult(success=False, source=model_path, error="model_adapter_not_implemented")

