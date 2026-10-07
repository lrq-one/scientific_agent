from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT / "workspace"
DEMO_DATA = ROOT / "data" / "demo"
SKILLS_ROOT = ROOT / "skills"
MAX_REPLANS = int(os.getenv("MAX_REPLANS", "2"))
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "16"))
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT", "120"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "500"))

