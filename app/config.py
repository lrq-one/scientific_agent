from pathlib import Path
import os


ROOT = Path(__file__).resolve().parents[1]


def _load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs from a local .env without overriding process env.

    The project intentionally keeps .env out of git. Windows User/Machine variables
    and explicitly exported shell variables always win over values in the file.
    """

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Test profiles must be explicit.  In particular, a plain pytest invocation
# must not recover a developer `.env` and silently connect to the running
# PostgreSQL/MinIO services.  Development/runtime processes retain the
# historical opt-in `.env` loading behaviour.
SCIENTIFIC_AGENT_TEST_PROFILE = os.getenv("SCIENTIFIC_AGENT_TEST_PROFILE", "").strip().lower()
if SCIENTIFIC_AGENT_TEST_PROFILE not in {"offline", "integration_isolated"}:
    _load_env_file(ROOT / ".env")

# Install a deny-by-default HTTP transport guard once.  It is inert for normal
# development/production profiles and becomes active dynamically when a test
# process selects offline or integration_isolated.  This protects direct
# provider construction as well as the normal factory paths.
from app.services.model_network_guard import install_model_network_guard
install_model_network_guard()

WORKSPACE_ROOT = ROOT / "workspace"
DEMO_DATA = ROOT / "data" / "demo"
SKILLS_ROOT = ROOT / "skills"

APP_MODE = os.getenv("APP_MODE", "development").strip().lower()
ENABLE_DEMO_DATA = os.getenv(
    "ENABLE_DEMO_DATA",
    "true" if APP_MODE in {"development", "dev", "demo"} else "false",
).strip().lower() in {"1", "true", "yes", "on"}

ALLOW_DETERMINISTIC_LLM_FALLBACK = os.getenv(
    "ALLOW_DETERMINISTIC_LLM_FALLBACK",
    # Explicit offline/isolated profiles must never silently fall back to a
    # deterministic fixture when a provider is configured: tests should expose
    # the blocked network or provider failure instead of simulating a model.
    "false" if SCIENTIFIC_AGENT_TEST_PROFILE in {"offline", "integration_isolated"}
    else ("true" if APP_MODE in {"development", "dev", "demo"} else "false"),
).strip().lower() in {"1", "true", "yes", "on"}

MAX_REPLANS = int(os.getenv("MAX_REPLANS", "2"))
MAX_TOOL_CALLS = int(os.getenv("MAX_TOOL_CALLS", "16"))
TASK_TIMEOUT = int(os.getenv("TASK_TIMEOUT", "240"))
MAX_ROWS = int(os.getenv("MAX_ROWS", "500"))
