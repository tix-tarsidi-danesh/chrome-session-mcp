"""Shared test setup: import the package from src/ with a throwaway cache dir."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CHROME_SESSION_MCP_CACHE"] = tempfile.mkdtemp(prefix="csm-test-")
os.environ["CHROME_SESSION_MCP_NO_BROWSER"] = "1"
os.environ.pop("CHROME_SESSION_MCP_LOGIN_WAIT", None)
sys.path.insert(0, os.path.join(ROOT, "src"))

from chrome_session_mcp import server  # noqa: E402,F401  (registers every tool and type)
from chrome_session_mcp.config import RUNTIME  # noqa: E402

EXAMPLES = os.path.join(ROOT, "examples")


def load(connectors, **extra):
    """Load a config dict into the shared runtime and return it."""
    return RUNTIME.load(dict(connectors=connectors, **extra))
