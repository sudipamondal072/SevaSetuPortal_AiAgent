import os


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}"
        )
    return value


PORTAL_BASE_URL = os.getenv(
    "PORTAL_BASE_URL",
    "http://localhost:8000",
).rstrip("/")
PORTAL_ISSUER_URL = os.getenv(
    "PORTAL_ISSUER_URL",
    PORTAL_BASE_URL,
).rstrip("/") + "/"

CITIZEN_MCP_PORT = int(os.getenv("CITIZEN_MCP_PORT", "8001"))
OFFICER_MCP_PORT = int(os.getenv("OFFICER_MCP_PORT", "8002"))

# In deployment, set these to the exact canonical resource URLs
# that the portal issues tokens for.
CITIZEN_RESOURCE_URL = os.getenv(
    "CITIZEN_RESOURCE_URL",
    "http://localhost:8001/mcp",
).rstrip("/")
OFFICER_RESOURCE_URL = os.getenv(
    "OFFICER_RESOURCE_URL",
    "http://localhost:8002/mcp",
).rstrip("/")

EVAL_RESULTS_FILE = os.getenv(
    "EVAL_RESULTS_FILE",
    "evals/results/results.json",
)