#!/bin/bash
# Setup script for Mac/Linux
set -e
cd "$(dirname "$0")"

echo "=== Workday MCP Setup ==="

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/python -m pip install -q -r requirements.txt

echo "Installing Playwright Chromium browser..."
.venv/bin/python -m playwright install chromium

INSTALL_DIR=$(cd "$(dirname "$0")" && pwd)

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Add this to your Claude Desktop config"
echo "(Settings > Developer > Edit Config):"
echo ""
echo '{
  "mcpServers": {
    "workday": {
      "command": "'$INSTALL_DIR'/.venv/bin/python",
      "args": ["'$INSTALL_DIR'/server.py"],
      "env": {
        "WORKDAY_BASE_URL": "https://wd115.myworkday.com",
        "WORKDAY_TENANT": "lilly"
      }
    }
  }
}'
echo ""
echo "Then restart Claude Desktop and say: 'Sign me in to Workday'"
