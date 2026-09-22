import os


ENGINE_PORT = int(os.getenv("ENGINE_PORT", "9000"))

INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")

WORKSPACE_ROOT = os.getenv(
    "DEPLOYMENT_WORKSPACE_ROOT",
    os.path.join(os.getcwd(), "deployment_workspaces"),
)