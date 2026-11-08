#!/bin/bash
# Start Workday MCP as a cloud server with a free Cloudflare tunnel.
# Share the printed URL with your team — they add it to their Claude config.
# Press Ctrl+C to stop everything.
set -e
cd "$(dirname "$0")"

PORT="${MCP_PORT:-8000}"

cleanup() {
    echo ""
    echo "Shutting down..."
    kill $MCP_PID $TUNNEL_PID 2>/dev/null
    wait $MCP_PID $TUNNEL_PID 2>/dev/null
    exit 0
}
trap cleanup INT TERM

echo "=== Workday MCP — Cloud Mode ==="
echo ""

# Start MCP server in HTTP mode
MCP_TRANSPORT=streamable-http \
MCP_PORT="$PORT" \
WORKDAY_BASE_URL="${WORKDAY_BASE_URL:-https://wd115.myworkday.com}" \
WORKDAY_TENANT="${WORKDAY_TENANT:-lilly}" \
.venv/bin/python3 server.py &
MCP_PID=$!

sleep 2

# Start Cloudflare tunnel
echo "Starting Cloudflare tunnel..."
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 &
TUNNEL_PID=$!

sleep 5

# Extract the tunnel URL from cloudflared output
echo ""
echo "=== Server is running ==="
echo ""
echo "Local:  http://localhost:$PORT/mcp"
echo "Tunnel: check the cloudflared output above for the https://...trycloudflare.com URL"
echo ""
echo "Team members add this to their Claude config:"
echo '  "workday": { "url": "https://<tunnel-url>/mcp" }'
echo ""
echo "Press Ctrl+C to stop."

wait
