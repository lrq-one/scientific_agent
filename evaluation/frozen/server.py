"""Isolated evaluation server; launch only with evaluation database URLs."""
import os
from pathlib import Path

if os.getenv("APP_MODE") != "evaluation" or os.getenv("AGENT_EVALUATION_ENABLED") != "1":
    raise RuntimeError("Evaluation server was not explicitly enabled")
if "phase4_eval" not in os.environ.get("ADMIN_DATABASE_URL", ""):
    raise RuntimeError("Evaluation server must not use the development database")

from evaluation.frozen.telemetry import install, IdentityMiddleware
install(Path(os.environ["EVALUATION_TELEMETRY_FILE"]), os.getenv("AGENT_EVALUATION_VARIANT", "FULL"))
from app.main import app
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5175"],
                   allow_methods=["*"], allow_headers=["*"], allow_credentials=True)
app.add_middleware(IdentityMiddleware)
