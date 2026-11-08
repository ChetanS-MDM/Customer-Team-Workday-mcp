"""
Configuration for the Workday MCP server.

All settings come from environment variables set in your Claude Desktop config.
Session data is stored in ~/.workday-mcp/ (created automatically).
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

# ---------------------------------------------------------------------------
# Session storage — persists across restarts
# ---------------------------------------------------------------------------
DATA_DIR = Path.home() / ".workday-mcp"

COOKIES_FILE = DATA_DIR / "cookies.json"
SESSION_FILE = DATA_DIR / "session.json"
STORAGE_STATE_FILE = DATA_DIR / "storage_state.json"
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"

# ---------------------------------------------------------------------------
# Environment variables — set in your Claude Desktop / Claude Code config
# ---------------------------------------------------------------------------
BASE_URL: str = (os.environ.get("WORKDAY_BASE_URL") or "").rstrip("/")
TENANT: str = os.environ.get("WORKDAY_TENANT") or ""
AUTH_TIMEOUT_SECONDS: int = int(os.environ.get("WORKDAY_AUTH_TIMEOUT_SECONDS") or "300")
CA_BUNDLE: str | None = os.environ.get("WORKDAY_CA_BUNDLE") or None
LOG_LEVEL: str = (os.environ.get("WORKDAY_LOG_LEVEL") or "INFO").upper()
ENABLE_RAW_GET: bool = (os.environ.get("WORKDAY_ENABLE_RAW_GET") or "").strip() == "1"

# ---------------------------------------------------------------------------
# Cloud deployment — set MCP_TRANSPORT=streamable-http to run as HTTP server
# ---------------------------------------------------------------------------
MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_PORT: int = int(os.environ.get("MCP_PORT", "8000"))

if not BASE_URL:
    raise SystemExit(
        "WORKDAY_BASE_URL is not set. Add it to your Claude Desktop config env, "
        "e.g. \"WORKDAY_BASE_URL\": \"https://wd115.myworkday.com\""
    )
if not TENANT:
    raise SystemExit(
        "WORKDAY_TENANT is not set. Add it to your Claude Desktop config env, "
        "e.g. \"WORKDAY_TENANT\": \"lilly\""
    )

# Derived URLs
HOME_URL: str = f"{BASE_URL}/{TENANT}/d/home.htmld"
USERINIT_URL: str = f"{BASE_URL}/wday/tlkw/rest/v11/tenant/{TENANT}/userinit?code="


def ensure_data_dir() -> None:
    """Create the data directory with owner-only permissions."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if platform.system() != "Windows":
        try:
            DATA_DIR.chmod(0o700)
        except OSError:
            pass
